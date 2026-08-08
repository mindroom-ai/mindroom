"""Own conversation identity and ingress envelope assembly for inbound turns."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, HOOK_SOURCE_KEY, SKIP_MENTIONS_KEY
from mindroom.dispatch_handoff import DispatchEvent, DispatchPayloadMetadata, PreparedTextEvent
from mindroom.dispatch_source import (
    IMAGE_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    content_owns_per_fire_thread_root,
    per_fire_thread_root_event_id_from_content,
    source_kind_from_content,
)
from mindroom.dispatch_thread_context import (
    DispatchThreadContext,
    context_with_dispatch_thread_context,
    planning_history_for,
    planning_history_unavailable_for,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import cached_room as matrix_cached_room
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.conversation_reads import (
    ConversationReader,
    ThreadReadMode,
    projected_thread_history,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import MatrixMediaEvent, is_audio_message_event, is_image_message_event
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadMembershipLookupError,
    ThreadResolution,
    ThreadResolutionState,
    resolve_event_thread_membership,
    resolve_related_event_thread_id_best_effort,
    thread_messages_thread_membership_access,
)
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.thread_utils import check_agent_mentioned
from mindroom.turn_origin import TurnOrigin, classify_turn_origin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.relation_lookup import RelationLookup


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
    thread_history: ThreadHistoryResult | None = None


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
        history: ThreadHistoryResult,
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
    conversation_reader: ConversationReader
    relations: RelationLookup


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
        source_kind_sender_is_trusted = self._sender_is_managed_entity(event.sender)
        if resolved_source_kind is None and isinstance(content, dict):
            source_kind_override = source_kind_from_content(content)
            if source_kind_override is not None and source_kind_sender_is_trusted:
                resolved_source_kind = source_kind_override
        if resolved_source_kind is None:
            if is_audio_message_event(event):
                resolved_source_kind = VOICE_SOURCE_KIND
            elif is_image_message_event(event):
                resolved_source_kind = IMAGE_SOURCE_KIND
            else:
                resolved_source_kind = MESSAGE_SOURCE_KIND

        resolved_hook_source: str | None = hook_source
        resolved_message_received_depth = message_received_depth or 0
        if isinstance(content, dict) and source_kind_sender_is_trusted:
            if resolved_hook_source is None:
                hook_source_override = content.get(HOOK_SOURCE_KEY)
                if isinstance(hook_source_override, str) and hook_source_override:
                    resolved_hook_source = hook_source_override
            if resolved_message_received_depth <= 0:
                depth_override = content.get(HOOK_MESSAGE_RECEIVED_DEPTH_KEY)
                if isinstance(depth_override, int) and not isinstance(depth_override, bool) and depth_override > 0:
                    resolved_message_received_depth = depth_override
        return resolved_source_kind, resolved_hook_source, resolved_message_received_depth

    def _turn_origin_for_event(
        self,
        *,
        event: DispatchEvent,
        requester_user_id: str,
        source_kind: str,
        original_sender: str | None,
        trusted_user_relay: bool,
    ) -> TurnOrigin:
        """Build canonical origin metadata for one inbound event envelope."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        trusted_human_relay = (
            trusted_user_relay
            and original_sender is not None
            and original_sender != ""
            and registry.current_entity_name_for_user_id(original_sender) is None
        )
        return classify_turn_origin(
            transport_sender_id=event.sender,
            requester_id=requester_user_id,
            sender_entity_name=registry.current_entity_name_for_user_id(event.sender),
            requester_entity_name=registry.current_entity_name_for_user_id(requester_user_id),
            source_kind=source_kind,
            original_sender=original_sender,
            trusted_user_relay=trusted_human_relay,
        )

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
        if event_source is not None:
            event_info = EventInfo.from_event(event_source)
            if event_info.can_be_thread_root and reply_to_event_id is not None:
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
            reply_to_event_id=reply_to_event_id,
            thread_start_root_event_id=thread_start_root_event_id,
            room_mode=effective_thread_mode == "room" and not automation_fire_root,
        )

    def build_message_envelope(
        self,
        *,
        event: DispatchEvent,
        requester_user_id: str,
        context: MessageContext,
        target: MessageTarget,
        attachment_ids: list[str] | None = None,
        agent_name: str | None = None,
        body: str | None = None,
        source_kind: str | None = None,
        dispatch_policy_source_kind: str | None = None,
        hook_source: str | None = None,
        message_received_depth: int | None = None,
        original_sender: str | None = None,
        trusted_user_relay: bool = False,
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
        registry = entity_identity_registry(config, self.deps.runtime_paths)

        return MessageEnvelope(
            source_event_id=event.event_id,
            target=target,
            body=body or event.body,
            attachment_ids=tuple(
                attachment_ids if attachment_ids is not None else parse_attachment_ids_from_event_source(event.source),
            ),
            mentioned_agents=tuple(
                registry.current_entity_name_for_user_id(agent_id.full_id) or agent_id.username
                for agent_id in context.mentioned_agents
            ),
            agent_name=agent_name or self.deps.agent_name,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
            origin=self._turn_origin_for_event(
                event=event,
                requester_user_id=requester_user_id,
                source_kind=resolved_source_kind,
                original_sender=original_sender,
                trusted_user_relay=trusted_user_relay,
            ),
        )

    def build_ingress_envelope(
        self,
        *,
        event: DispatchEvent,
        requester_user_id: str,
        target: MessageTarget,
        attachment_ids: list[str] | None = None,
        agent_name: str | None = None,
        body: str | None = None,
        source_kind: str | None = None,
        dispatch_policy_source_kind: str | None = None,
        hook_source: str | None = None,
        message_received_depth: int | None = None,
        original_sender: str | None = None,
        trusted_user_relay: bool = False,
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
            target=target,
            body=body or event.body,
            attachment_ids=tuple(
                attachment_ids if attachment_ids is not None else parse_attachment_ids_from_event_source(event.source),
            ),
            mentioned_agents=(),
            agent_name=agent_name or self.deps.agent_name,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
            origin=self._turn_origin_for_event(
                event=event,
                requester_user_id=requester_user_id,
                source_kind=resolved_source_kind,
                original_sender=original_sender,
                trusted_user_relay=trusted_user_relay,
            ),
        )

    async def coalescing_thread_id(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
    ) -> str | None:
        """Return the coalescing thread scope for one inbound event."""
        config = self.deps.runtime.config
        event_info = EventInfo.from_event(event.source)
        if (
            config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            == "room"
        ):
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
        if resolution.state is ThreadResolutionState.INDETERMINATE and resolution.candidate_thread_root_id is not None:
            # A non-blocking read cannot prove a root in a conversation it has
            # never hydrated, and a plain reply to an unthreaded message is the
            # ordinary way to reach one. Unlike thread-context resolution, which
            # repairs an unproven candidate a moment later, coalescing has no
            # later stage to correct a wrong key in -- the batch is formed here.
            # So the same repair happens here, and it costs nothing extra: it is
            # the walk this turn was about to do anyway, and `ensure_hydrated`
            # shares one of those per conversation.
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
        try:
            return await resolve_event_thread_membership(
                room.room_id,
                event_info,
                event_id=event.event_id,
                access=self._thread_membership_access(
                    mode=mode,
                    requires_complete_history=True,
                ),
            )
        except Exception as exc:
            msg = f"Could not resolve canonical coalescing thread for {event.event_id}"
            raise ThreadMembershipLookupError(msg) from exc

    async def _explicit_thread_id_for_event(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        mode: ThreadReadMode,
    ) -> _ThreadIdLookup:
        """Resolve thread membership and identify unproven dispatch candidates."""
        access = self._thread_membership_access(
            mode=mode,
            requires_complete_history=True,
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
        if mode is ThreadReadMode.STRICT:
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
    ) -> str | None:
        """Return thread membership from local state without waiting on the homeserver."""
        return await resolve_related_event_thread_id_best_effort(
            room_id,
            related_event_id,
            access=self._thread_membership_access(
                mode=ThreadReadMode.NONBLOCKING,
                # Reaction hook context wants the target's thread, not its
                # conversation, so an incomplete page answers it just as well.
                requires_complete_history=False,
            ),
        )

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
            fetch_event_info=self._event_info_for_event_id,
            fetch_thread_messages=lambda room_id, thread_id: self._read_thread_messages(
                room_id,
                thread_id,
                mode=mode,
                requires_complete_history=requires_complete_history,
            ),
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
        # A non-blocking read runs before the turn is accepted, so it never
        # waits on the homeserver; a strict read feeds a prompt or a root
        # proof, both of which are wrong when a message is missing, so it
        # blocks.
        strict = mode is ThreadReadMode.STRICT
        reader = self.deps.conversation_reader
        # A non-blocking read is still complete when the conversation was
        # already hydrated and nothing is pending -- completeness is a property
        # of what is known, not of how hard the caller was willing to work for
        # it. Claiming otherwise would send every dispatch through a redundant
        # strict re-read of a conversation it had already proven whole.
        source_degraded = (
            not strict
            and requires_complete_history
            and await reader.may_have_unread_history(room_id=room_id, thread_id=thread_id)
        )
        page = await (reader.read_strict if strict else reader.read)(
            room_id=room_id,
            thread_id=thread_id,
            limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
        )
        # Two independent ways this page can fall short of the conversation.
        # `source_degraded` means local state might not have caught up yet, and
        # a strict read rules it out. Hydration stopping at a ceiling is not
        # ruled out by anything a caller can do: the walk already ran, it
        # already installed its marker, and it will not run again under this
        # membership. Only the recorded flag distinguishes a page that ends
        # because the conversation does from one that ends because the walk
        # ran out of allowance.
        hydration_truncated = await reader.hydration_was_truncated(room_id=room_id, thread_id=thread_id)
        return projected_thread_history(
            page,
            complete=not hydration_truncated and not source_degraded,
            source_degraded=source_degraded,
        )

    async def _event_info_for_event_id(
        self,
        room_id: str,
        event_id: str,
    ) -> EventInfo | None:
        return await self.deps.relations.event_info(room_id, event_id)

    async def _resolve_thread_context(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        mode: ThreadReadMode,
    ) -> _ThreadContextLookup:
        """Resolve one thread context using either snapshot or full history."""
        thread_lookup = await self._explicit_thread_id_for_event(
            room_id,
            event_id,
            event_info,
            mode=mode,
        )
        thread_id = thread_lookup.thread_id
        if thread_id is None:
            candidate_thread_root_id = thread_lookup.candidate_thread_root_id
            candidate_history = thread_lookup.thread_history
            if (
                candidate_thread_root_id is not None
                and candidate_history is not None
                and mode is ThreadReadMode.NONBLOCKING
                and is_thread_history_degraded(candidate_history)
            ):
                strict_lookup = await self._explicit_thread_id_for_event(
                    room_id,
                    event_id,
                    event_info,
                    mode=ThreadReadMode.STRICT,
                )
                if strict_lookup.thread_id is not None:
                    thread_lookup = strict_lookup
                    thread_id = strict_lookup.thread_id
                elif strict_lookup.thread_history is None:
                    return _ThreadContextLookup.unproven_candidate_without_history(
                        candidate_thread_root_id,
                    )
                else:
                    return _ThreadContextLookup.unproven_candidate_demoted(
                        candidate_thread_root_id,
                        strict_lookup.thread_history,
                    )
            if thread_id is None:
                if candidate_thread_root_id is None:
                    return _ThreadContextLookup.room_level()
                if candidate_history is None:
                    return _ThreadContextLookup.unproven_candidate_without_history(
                        candidate_thread_root_id,
                    )
                return _ThreadContextLookup.unproven_candidate_demoted(
                    candidate_thread_root_id,
                    candidate_history,
                )

        thread_messages = thread_lookup.thread_history
        if thread_messages is None:
            thread_messages = await self._read_thread_messages(
                room_id,
                thread_id,
                mode=mode,
            )
        if mode is ThreadReadMode.NONBLOCKING and is_thread_history_degraded(thread_messages):
            # Proven threads must not plan from degraded history; wait for Matrix-backed refill.
            thread_messages = await self._read_thread_messages(
                room_id,
                thread_id,
                mode=ThreadReadMode.STRICT,
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
        mode: ThreadReadMode = ThreadReadMode.NONBLOCKING,
    ) -> DispatchContextResult:
        """Extract dispatch context, escalating to a strict read when a non-blocking one degrades."""
        context, thread_context = await self._extract_message_context_parts(
            room,
            event,
            mode=mode,
            include_dispatch_context=True,
            payload_metadata=payload_metadata,
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
            # The relay's own ``m.thread`` relation, and nothing else. This context skips the
            # canonical resolver on purpose, so a thread named inside an ``m.new_content`` is the
            # only value here that no lookup would ever confirm - and Matrix ignores it anyway,
            # placing an edit by the event it replaces. Believing it would hand whoever wrote the
            # relayed payload the choice of which thread this response appears in.
            resolved_thread_id = EventInfo.from_event(resolved_event_source).thread_id
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

    async def extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> MessageContext:
        """Extract strict message context for one inbound turn."""
        context, _thread_context = await self._extract_message_context_parts(
            room,
            event,
            mode=ThreadReadMode.STRICT,
            include_dispatch_context=False,
            payload_metadata=payload_metadata,
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

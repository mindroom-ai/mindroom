"""Shared Matrix thread mutation policy.

Ownership map:
- canonical resolution: `mindroom.matrix.thread_membership`
- mutation/bookkeeping impact: this module
- tool-facing root normalization: `mindroom.custom_tools.attachment_helpers`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, cast

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadMembershipLookupError,
    ThreadResolution,
    ThreadResolutionState,
    ThreadRootProof,
    fetch_event_info_for_client,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
)
from mindroom.matrix.thread_projection import resolve_thread_ids_for_event_infos
from mindroom.matrix.thread_room_scan import (
    RoomScanConversationCache,
    _fetch_event_info_from_conversation_cache,
    _room_scan_membership_access_for_client,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


def is_thread_affecting_relation(event_info: EventInfo) -> bool:
    """Return whether one room message relation can affect thread-scoped cache state."""
    return (
        event_info.is_thread or event_info.is_edit or event_info.is_reply or event_info.relation_type == "m.reference"
    )


def _redaction_can_affect_thread_cache(event_info: EventInfo) -> bool:
    """Return whether redacting one related event can invalidate cached thread messages."""
    return not event_info.is_reaction


class MutationThreadImpactState(Enum):
    """Mutation outcomes for one event relation."""

    THREADED = auto()
    ROOM_LEVEL = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class MutationThreadImpact:
    """Classify how one mutation should affect thread state."""

    state: MutationThreadImpactState
    thread_id: str | None = None

    @classmethod
    def threaded(cls, thread_id: str) -> MutationThreadImpact:
        """Return one mutation impact that definitely targets one thread."""
        return cls(MutationThreadImpactState.THREADED, thread_id=thread_id)

    @classmethod
    def room_level(cls) -> MutationThreadImpact:
        """Return one mutation impact that is definitely room-level."""
        return cls(MutationThreadImpactState.ROOM_LEVEL)

    @classmethod
    def unknown(cls) -> MutationThreadImpact:
        """Return one mutation impact that must fail closed through room invalidation."""
        return cls(MutationThreadImpactState.UNKNOWN)


@dataclass
class MutationResolutionContext:
    """Cache-backed lookup context reused across one mutation batch."""

    page_event_infos: dict[str, EventInfo]
    page_resolved_thread_ids: dict[str, str]
    cached_thread_ids: dict[str, str | None] = field(default_factory=dict)
    cached_event_infos: dict[str, EventInfo] = field(default_factory=dict)
    cached_thread_root_proofs: dict[str, ThreadRootProof] = field(default_factory=dict)


def _mutation_thread_impact_from_resolution(
    resolution: ThreadResolution,
) -> MutationThreadImpact:
    """Map canonical membership results onto mutation behavior."""
    if resolution.state is ThreadResolutionState.THREADED:
        assert resolution.thread_id is not None
        return MutationThreadImpact.threaded(resolution.thread_id)
    if resolution.state is ThreadResolutionState.ROOM_LEVEL:
        return MutationThreadImpact.room_level()
    return MutationThreadImpact.unknown()


async def resolve_event_thread_impact_for_client(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_type: str,
    content: Mapping[str, object],
    conversation_cache: RoomScanConversationCache | None,
) -> MutationThreadImpact:
    """Return the mutation impact for one outbound client-side event payload."""
    if event_type != "m.room.message":
        return MutationThreadImpact.room_level()
    event_info = EventInfo.from_event({"type": event_type, "content": dict(content)})
    resolution = await resolve_event_thread_membership(
        room_id,
        event_info,
        access=_room_scan_membership_access_for_client(
            client,
            conversation_cache=conversation_cache,
        ),
    )
    return _mutation_thread_impact_from_resolution(resolution)


async def resolve_redaction_thread_impact_for_client(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_id: str,
    conversation_cache: RoomScanConversationCache | None,
) -> MutationThreadImpact:
    """Return the mutation impact for one client-side redaction target."""
    if conversation_cache is None:
        target_event_info = await fetch_event_info_for_client(
            client,
            room_id,
            event_id,
            strict=True,
        )
    else:
        target_event_info = await _fetch_event_info_from_conversation_cache(
            conversation_cache,
            room_id,
            event_id,
            strict=True,
        )
    if target_event_info is not None and target_event_info.is_reaction:
        return MutationThreadImpact.room_level()
    resolution = await resolve_related_event_thread_membership(
        room_id,
        event_id,
        access=_room_scan_membership_access_for_client(
            client,
            conversation_cache=conversation_cache,
        ),
    )
    return _mutation_thread_impact_from_resolution(resolution)


class ThreadMutationResolver:
    """Own thread-membership resolution for thread cache mutations."""

    def __init__(
        self,
        *,
        logger_getter: Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        fetch_event_info_for_thread_resolution: Callable[[str, str], Awaitable[EventInfo | None]],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self._fetch_event_info_for_thread_resolution = fetch_event_info_for_thread_resolution

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    async def build_sync_mutation_resolution_context(
        self,
        room_id: str,
        *,
        plain_events: Sequence[dict[str, object]],
        threaded_events: Sequence[dict[str, object]],
    ) -> MutationResolutionContext:
        """Build one page-local resolution context for a sync batch."""
        page_event_infos: dict[str, EventInfo] = {}
        ordered_event_ids: list[str] = []
        for event_source in [*plain_events, *threaded_events]:
            event_id = event_source.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                continue
            page_event_infos[event_id] = EventInfo.from_event(event_source)
            ordered_event_ids.append(event_id)
        page_resolved_thread_ids = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=page_event_infos,
            ordered_event_ids=ordered_event_ids,
        )
        return MutationResolutionContext(
            page_event_infos=page_event_infos,
            page_resolved_thread_ids=page_resolved_thread_ids,
        )

    async def resolve_redaction_thread_impact(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        failure_message: str,
        event_id: str | None = None,
        resolution_context: MutationResolutionContext | None = None,
    ) -> MutationThreadImpact:
        """Resolve how one redaction should affect thread cache state."""
        try:
            try:
                target_event_info = await self._event_info_for_mutation_context(
                    room_id,
                    redacted_event_id,
                    resolution_context=resolution_context,
                )
            except ThreadMembershipLookupError:
                return MutationThreadImpact.unknown()
            if not _redaction_can_affect_thread_cache(target_event_info):
                return MutationThreadImpact.room_level()
            resolution = await resolve_related_event_thread_membership(
                room_id,
                redacted_event_id,
                access=self._thread_membership_access(
                    room_id=room_id,
                    resolution_context=resolution_context,
                ),
            )
            return _mutation_thread_impact_from_resolution(resolution)
        except Exception as exc:
            self.logger.warning(
                failure_message,
                room_id=room_id,
                event_id=event_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            return MutationThreadImpact.unknown()

    async def resolve_thread_impact_for_mutation(
        self,
        room_id: str,
        *,
        event_info: EventInfo,
        event_id: str | None,
        context: str,
        resolution_context: MutationResolutionContext | None = None,
    ) -> MutationThreadImpact:
        """Resolve how one message mutation should affect thread cache state."""
        explicit_thread_id = event_info.thread_id or event_info.thread_id_from_edit
        if explicit_thread_id is not None:
            return MutationThreadImpact.threaded(explicit_thread_id)
        try:
            resolution = await resolve_event_thread_membership(
                room_id,
                event_info,
                event_id=event_id,
                access=self._thread_membership_access(
                    room_id=room_id,
                    resolution_context=resolution_context,
                ),
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to resolve cached thread for mutation",
                room_id=room_id,
                event_id=event_id,
                original_event_id=event_info.original_event_id,
                context=context,
                error=str(exc),
            )
            return MutationThreadImpact.unknown()
        return _mutation_thread_impact_from_resolution(resolution)

    async def _lookup_thread_id_for_mutation_context(
        self,
        room_id: str,
        event_id: str,
        *,
        resolution_context: MutationResolutionContext | None,
    ) -> str | None:
        if resolution_context is not None:
            if event_id in resolution_context.page_resolved_thread_ids:
                return resolution_context.page_resolved_thread_ids[event_id]
            if event_id in resolution_context.cached_thread_ids:
                return resolution_context.cached_thread_ids[event_id]
        thread_id = await self.runtime.event_cache.get_thread_id_for_event(room_id, event_id)
        if resolution_context is not None:
            resolution_context.cached_thread_ids[event_id] = thread_id
        return thread_id

    async def _event_info_for_mutation_context(
        self,
        room_id: str,
        event_id: str,
        *,
        resolution_context: MutationResolutionContext | None,
    ) -> EventInfo:
        if resolution_context is not None:
            page_event_info = resolution_context.page_event_infos.get(event_id)
            if page_event_info is not None:
                return page_event_info
            cached_event_info = resolution_context.cached_event_infos.get(event_id)
            if cached_event_info is not None:
                return cached_event_info
        event_info = await self._fetch_event_info_for_thread_resolution(room_id, event_id)
        if event_info is None:
            msg = f"Thread membership lookup unavailable for {event_id}"
            raise ThreadMembershipLookupError(msg)
        if resolution_context is not None:
            resolution_context.cached_event_infos[event_id] = event_info
        return event_info

    async def _prove_thread_root_for_mutation_context(
        self,
        room_id: str,
        thread_root_id: str,
        *,
        resolution_context: MutationResolutionContext | None,
    ) -> ThreadRootProof:
        if resolution_context is not None:
            cached_proof = resolution_context.cached_thread_root_proofs.get(thread_root_id)
            if cached_proof is not None:
                return cached_proof
            if any(
                _page_event_info_counts_as_thread_child_proof(
                    thread_root_id,
                    event_id=event_id,
                    event_info=event_info,
                )
                for event_id, event_info in resolution_context.page_event_infos.items()
            ):
                proof = ThreadRootProof.proven()
                resolution_context.cached_thread_root_proofs[thread_root_id] = proof
                return proof
        try:
            thread_events = await self.runtime.event_cache.get_thread_events(room_id, thread_root_id)
        except Exception as exc:
            return ThreadRootProof.proof_unavailable(exc)
        if thread_events is None:
            proof = ThreadRootProof.proof_unavailable(
                ThreadMembershipLookupError(f"Thread root proof unavailable for {thread_root_id}"),
            )
        else:
            has_children = any(
                _event_source_counts_as_thread_child_proof(
                    thread_root_id,
                    event_source=cast("dict[str, object]", event_source),
                )
                for event_source in thread_events
            )
            proof = ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()
        if resolution_context is not None:
            resolution_context.cached_thread_root_proofs[thread_root_id] = proof
        return proof

    def _thread_membership_access(
        self,
        *,
        room_id: str,
        resolution_context: MutationResolutionContext | None,
    ) -> ThreadMembershipAccess:
        """Return the mutation-time thread-membership accessors without room scans."""

        async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
            return await self._lookup_thread_id_for_mutation_context(
                room_id,
                event_id,
                resolution_context=resolution_context,
            )

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo:
            return await self._event_info_for_mutation_context(
                room_id,
                event_id,
                resolution_context=resolution_context,
            )

        async def prove_thread_root(_room_id: str, thread_root_id: str) -> ThreadRootProof:
            return await self._prove_thread_root_for_mutation_context(
                room_id,
                thread_root_id,
                resolution_context=resolution_context,
            )

        return ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
        )


def _event_source_counts_as_thread_child_proof(
    thread_root_id: str,
    *,
    event_source: dict[str, object],
) -> bool:
    """Return whether one cached event proves a root has real thread children."""
    event_id = event_source.get("event_id")
    if event_id == thread_root_id:
        return False
    event_info = EventInfo.from_event(event_source)
    if event_info.is_edit and event_info.original_event_id == thread_root_id:
        return False
    return isinstance(event_info.thread_id, str) and event_info.thread_id == thread_root_id


def _page_event_info_counts_as_thread_child_proof(
    thread_root_id: str,
    *,
    event_id: str,
    event_info: EventInfo,
) -> bool:
    """Return whether one page-local event proves a root has thread children."""
    if event_id == thread_root_id:
        return False
    return any(
        candidate_thread_id == thread_root_id
        for candidate_thread_id in (
            event_info.thread_id,
            event_info.thread_id_from_edit,
        )
    )

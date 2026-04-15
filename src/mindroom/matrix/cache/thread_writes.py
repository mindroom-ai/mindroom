"""Thread mutation and advisory bookkeeping policy for Matrix conversation cache."""

from __future__ import annotations

import asyncio
import time
import typing
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.cache.event_cache import normalize_event_source_for_cache, normalize_nio_event_for_cache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadMembershipLookupError,
    ThreadResolution,
    ThreadResolutionState,
    ThreadRootProof,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
    resolve_thread_ids_for_event_infos,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


def _collect_sync_timeline_cache_updates(
    room_id: str,
    event: nio.Event,
    *,
    room_threaded_events: dict[str, list[dict[str, object]]],
    room_plain_events: dict[str, list[dict[str, object]]],
    room_redactions: dict[str, list[str]],
) -> None:
    event_source = event.source if isinstance(event.source, dict) else {}
    if isinstance(event, nio.RedactionEvent):
        redacted_event_id = event.redacts
        if isinstance(redacted_event_id, str) and redacted_event_id:
            room_redactions.setdefault(room_id, []).append(redacted_event_id)
        return

    event_info = EventInfo.from_event(event_source)
    if _is_thread_affecting_relation(event_info):
        cache_update = _threaded_sync_event_cache_update(room_id, event)
        if cache_update is None:
            return
        update_room_id, normalized_event_source = cache_update
        room_threaded_events.setdefault(update_room_id, []).append(normalized_event_source)
        return

    cache_update = _collect_sync_event_cache_update(room_id, event)
    if cache_update is None:
        return
    update_room_id, normalized_event_source = cache_update
    room_plain_events.setdefault(update_room_id, []).append(normalized_event_source)


def _collect_sync_event_cache_update(
    room_id: str,
    event: nio.Event,
) -> tuple[str, dict[str, object]] | None:
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    return room_id, normalize_nio_event_for_cache(event)


def _threaded_sync_event_cache_update(
    room_id: str,
    event: nio.Event,
) -> tuple[str, dict[str, object]] | None:
    event_source = event.source if isinstance(event.source, dict) else {}
    event_info = EventInfo.from_event(event_source)
    if not _is_thread_affecting_relation(event_info):
        return None
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    return room_id, normalize_nio_event_for_cache(event)


def _has_explicit_thread_relation(event_info: EventInfo) -> bool:
    return isinstance(event_info.thread_id, str) or isinstance(event_info.thread_id_from_edit, str)


def _is_thread_affecting_relation(event_info: EventInfo) -> bool:
    """Return whether one room message relation can affect thread-scoped cache state."""
    return (
        event_info.is_thread or event_info.is_edit or event_info.is_reply or event_info.relation_type == "m.reference"
    )


def _redaction_can_affect_thread_cache(event_info: EventInfo) -> bool:
    """Return whether redacting one related event can invalidate cached thread messages."""
    return not event_info.is_reaction


def _mutation_thread_impact_from_resolution(
    resolution: ThreadResolution,
) -> MutationThreadImpact:
    """Map canonical membership results onto cache-mutation behavior."""
    if resolution.state is ThreadResolutionState.THREADED:
        assert resolution.thread_id is not None
        return MutationThreadImpact.threaded(resolution.thread_id)
    if resolution.state is ThreadResolutionState.ROOM_LEVEL:
        return MutationThreadImpact.room_level()
    return MutationThreadImpact.unknown()


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


class MutationThreadImpactState(Enum):
    """Cache-mutation outcomes for one event relation."""

    THREADED = auto()
    ROOM_LEVEL = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class MutationThreadImpact:
    """Classify how one cache mutation should affect thread state."""

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
class _MutationResolutionContext:
    """Cache-backed lookup context reused across one mutation batch."""

    page_event_infos: dict[str, EventInfo]
    page_resolved_thread_ids: dict[str, str]
    cached_thread_ids: dict[str, str | None] = field(default_factory=dict)
    cached_event_infos: dict[str, EventInfo] = field(default_factory=dict)
    cached_thread_root_proofs: dict[str, ThreadRootProof] = field(default_factory=dict)


class ThreadWritePolicy:
    """Own thread-affecting cache mutations and outbound advisory bookkeeping."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        require_client: typing.Callable[[], nio.AsyncClient],
        fetch_event_info_for_thread_resolution: typing.Callable[[str, str], typing.Awaitable[EventInfo | None]],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self.require_client = require_client
        self._fetch_event_info_for_thread_resolution = fetch_event_info_for_thread_resolution

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    def _cache_runtime_available(self) -> bool:
        return self.runtime.event_cache is not None and self.runtime.event_cache_write_coordinator is not None

    def _queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> asyncio.Task[object]:
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_room_update(room_id, update_coro_factory, name=name)

    def _disable_cache_after_fail_closed_invalidation(
        self,
        *,
        room_id: str,
        reason: str,
        scope: str,
    ) -> None:
        self.runtime.event_cache.disable(f"stale_marker_failed:{scope}:{room_id}:{reason}")

    async def _fail_closed_thread_invalidation(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
        stale_marker_error: Exception,
    ) -> None:
        try:
            await self.runtime.event_cache.invalidate_thread(room_id, thread_id)
        except Exception as invalidate_exc:
            self.logger.warning(
                "Failed to delete cached thread rows after stale-marker failure; disabling cache",
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
                stale_marker_error=str(stale_marker_error),
                error=str(invalidate_exc),
            )
        else:
            return
        self._disable_cache_after_fail_closed_invalidation(
            room_id=room_id,
            reason=reason,
            scope=f"thread:{thread_id}",
        )

    async def _fail_closed_room_invalidation(
        self,
        room_id: str,
        *,
        reason: str,
        stale_marker_error: Exception,
    ) -> None:
        try:
            await self.runtime.event_cache.invalidate_room_threads(room_id)
        except Exception as invalidate_exc:
            self.logger.warning(
                "Failed to delete cached room thread rows after stale-marker failure; disabling cache",
                room_id=room_id,
                reason=reason,
                stale_marker_error=str(stale_marker_error),
                error=str(invalidate_exc),
            )
        else:
            return
        self._disable_cache_after_fail_closed_invalidation(
            room_id=room_id,
            reason=reason,
            scope="room",
        )

    async def _resolve_redaction_thread_impact(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        failure_message: str,
        event_id: str | None = None,
        resolution_context: _MutationResolutionContext | None = None,
    ) -> MutationThreadImpact:
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

    async def _redact_cached_event(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        thread_id: str | None,
        failure_message: str,
    ) -> bool:
        try:
            return bool(await self.runtime.event_cache.redact_event(room_id, redacted_event_id))
        except Exception as exc:
            self.logger.warning(
                failure_message,
                room_id=room_id,
                thread_id=thread_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            return False

    async def _invalidate_after_redaction(
        self,
        room_id: str,
        *,
        impact: MutationThreadImpact,
        redacted: bool,
        success_reason: str,
        failure_reason: str,
        lookup_unavailable_reason: str,
    ) -> None:
        if impact.state is MutationThreadImpactState.THREADED:
            assert impact.thread_id is not None
            await self._invalidate_known_thread(
                room_id,
                impact.thread_id,
                reason=success_reason if redacted else failure_reason,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            await self._invalidate_room_threads(room_id, reason=lookup_unavailable_reason)

    async def _invalidate_known_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
    ) -> None:
        try:
            await self.runtime.event_cache.mark_thread_stale(room_id, thread_id, reason=reason)
        except Exception as exc:
            self.logger.warning(
                "Failed to mark cached thread stale",
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
                error=str(exc),
            )
            await self._fail_closed_thread_invalidation(
                room_id,
                thread_id,
                reason=reason,
                stale_marker_error=exc,
            )

    async def _invalidate_room_threads(
        self,
        room_id: str,
        *,
        reason: str,
    ) -> None:
        try:
            await self.runtime.event_cache.mark_room_threads_stale(room_id, reason=reason)
        except Exception as exc:
            self.logger.warning(
                "Failed to mark cached room threads stale",
                room_id=room_id,
                reason=reason,
                error=str(exc),
            )
            await self._fail_closed_room_invalidation(
                room_id,
                reason=reason,
                stale_marker_error=exc,
            )

    async def _build_sync_mutation_resolution_context(
        self,
        room_id: str,
        *,
        plain_events: Sequence[dict[str, object]],
        threaded_events: Sequence[dict[str, object]],
    ) -> _MutationResolutionContext:
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
        return _MutationResolutionContext(
            page_event_infos=page_event_infos,
            page_resolved_thread_ids=page_resolved_thread_ids,
        )

    async def _lookup_thread_id_for_mutation_context(
        self,
        room_id: str,
        event_id: str,
        *,
        resolution_context: _MutationResolutionContext | None,
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
        resolution_context: _MutationResolutionContext | None,
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
        resolution_context: _MutationResolutionContext | None,
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
                    event_source=typing.cast("dict[str, object]", event_source),
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
        resolution_context: _MutationResolutionContext | None,
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

    async def _resolve_thread_impact_for_mutation(
        self,
        room_id: str,
        *,
        event_info: EventInfo,
        event_id: str | None,
        context: str,
        resolution_context: _MutationResolutionContext | None = None,
    ) -> MutationThreadImpact:
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

    async def _append_event_to_cache(
        self,
        room_id: str,
        thread_id: str,
        event_source: dict[str, Any],
        *,
        context: str,
    ) -> bool:
        event_id = event_source.get("event_id")
        try:
            appended = await self.runtime.event_cache.append_event(room_id, thread_id, event_source)
        except Exception as exc:
            self.logger.warning(
                "Failed to append thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
                error=str(exc),
            )
            return False
        if not appended:
            self.logger.debug(
                "Skipping thread event append because raw thread cache is missing",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
            )
        return bool(appended)

    async def _apply_outbound_message_notification(
        self,
        room_id: str,
        event_id: str,
        event_source: dict[str, Any],
        event_info: EventInfo,
    ) -> None:
        impact = await self._resolve_thread_impact_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event_id,
            context="outbound",
        )
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self.logger.debug(
                "Skipping outbound thread cache bookkeeping for non-threaded message mutation",
                room_id=room_id,
                event_id=event_id,
                original_event_id=event_info.original_event_id,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            await self._invalidate_room_threads(
                room_id,
                reason="outbound_thread_lookup_unavailable",
            )
            return
        assert impact.thread_id is not None
        await self._invalidate_known_thread(
            room_id,
            impact.thread_id,
            reason="outbound_thread_mutation",
        )
        await self._append_event_to_cache(
            room_id,
            impact.thread_id,
            event_source,
            context="outbound",
        )

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule advisory bookkeeping for one locally sent threaded message or edit."""
        if not self._cache_runtime_available():
            return
        if not isinstance(event_id, str) or not event_id:
            return

        client = self.require_client()
        sender = client.user_id if isinstance(client.user_id, str) else None
        origin_server_ts = int(time.time() * 1000)
        event_source = normalize_event_source_for_cache(
            {
                "type": "m.room.message",
                "room_id": room_id,
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": origin_server_ts,
                "content": dict(content),
            },
            event_id=event_id,
            sender=sender,
            origin_server_ts=origin_server_ts,
        )
        event_info = EventInfo.from_event(event_source)
        is_thread_candidate = _is_thread_affecting_relation(event_info)
        if not is_thread_candidate:
            return

        async def safe_update() -> None:
            try:
                await self._apply_outbound_message_notification(room_id, event_id, event_source, event_info)
            except asyncio.CancelledError as exc:
                self.logger.warning(
                    "Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
                )
            except Exception as exc:
                self.logger.warning(
                    "Ignoring outbound threaded message cache bookkeeping failure after successful send",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
                )

        try:
            self._queue_room_cache_update(
                room_id,
                safe_update,
                name="matrix_cache_notify_outbound_message",
            )
        except asyncio.CancelledError as exc:
            self.logger.warning(
                "Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound threaded message cache bookkeeping failure after successful send",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )

    async def _apply_outbound_redaction_notification(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        impact = await self._resolve_redaction_thread_impact(
            room_id,
            redacted_event_id,
            failure_message="Ignoring outbound Matrix redaction cache lookup failure after successful redact",
        )
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self.logger.debug(
                "Skipping outbound thread cache bookkeeping for non-threaded redaction",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
            )
            return
        thread_id = impact.thread_id
        redacted = await self._redact_cached_event(
            room_id,
            redacted_event_id,
            thread_id=thread_id,
            failure_message="Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
        )
        await self._invalidate_after_redaction(
            room_id,
            impact=impact,
            redacted=redacted,
            success_reason="outbound_redaction",
            failure_reason="outbound_redaction_failed",
            lookup_unavailable_reason="outbound_redaction_lookup_unavailable",
        )

    def notify_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Schedule advisory bookkeeping for one locally redacted threaded message."""
        if not self._cache_runtime_available():
            return
        if not redacted_event_id:
            return

        async def safe_update() -> None:
            try:
                await self._apply_outbound_redaction_notification(room_id, redacted_event_id)
            except asyncio.CancelledError as exc:
                self.logger.warning(
                    "Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )
            except Exception as exc:
                self.logger.warning(
                    "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )

        try:
            self._queue_room_cache_update(
                room_id,
                safe_update,
                name="matrix_cache_notify_outbound_redaction",
            )
        except asyncio.CancelledError as exc:
            self.logger.warning(
                "Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        if not self._cache_runtime_available():
            return

        impact = await self._resolve_thread_impact_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event.event_id,
            context="live",
        )
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self.logger.debug(
                "Skipping live thread cache bookkeeping for known non-threaded message mutation",
                room_id=room_id,
                event_id=event.event_id,
                original_event_id=event_info.original_event_id,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            await self._invalidate_room_threads(
                room_id,
                reason="live_thread_lookup_unavailable",
            )
            return
        assert impact.thread_id is not None
        thread_id = impact.thread_id

        event_source = normalize_nio_event_for_cache(event)

        async def append_and_invalidate() -> bool:
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason="live_thread_mutation",
            )
            appended = await self._append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="live",
            )
            if not appended:
                await self._invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="live_append_failed",
                )
            return appended

        await self._queue_room_cache_update(
            room_id,
            append_and_invalidate,
            name="matrix_cache_append_live_event",
        )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        if not self._cache_runtime_available():
            return
        impact = await self._resolve_redaction_thread_impact(
            room_id,
            event.redacts,
            failure_message="Failed to resolve cached thread for redaction",
            event_id=event.event_id,
        )
        thread_id = impact.thread_id

        async def redact_and_invalidate() -> bool:
            redacted = await self._redact_cached_event(
                room_id,
                event.redacts,
                thread_id=thread_id,
                failure_message="Failed to apply live redaction to cache",
            )
            await self._invalidate_after_redaction(
                room_id,
                impact=impact,
                redacted=redacted,
                success_reason="live_redaction",
                failure_reason="live_redaction_failed",
                lookup_unavailable_reason="live_redaction_lookup_unavailable",
            )
            return redacted

        await self._queue_room_cache_update(
            room_id,
            redact_and_invalidate,
            name="matrix_cache_apply_redaction",
        )

    async def _persist_threaded_sync_events(
        self,
        room_id: str,
        threaded_events: Sequence[dict[str, object]],
        *,
        resolution_context: _MutationResolutionContext,
    ) -> None:
        room_threads_invalidated = False
        for event_source in threaded_events:
            event_info = EventInfo.from_event(event_source)
            event_id = event_source.get("event_id")
            impact = await self._resolve_thread_impact_for_mutation(
                room_id,
                event_info=event_info,
                event_id=event_id if isinstance(event_id, str) else None,
                context="sync",
                resolution_context=resolution_context,
            )
            if impact.state is MutationThreadImpactState.ROOM_LEVEL:
                self.logger.debug(
                    "Skipping sync thread cache bookkeeping for known non-threaded message mutation",
                    room_id=room_id,
                    event_id=event_id,
                    original_event_id=event_info.original_event_id,
                )
                continue
            if impact.state is MutationThreadImpactState.UNKNOWN:
                if not room_threads_invalidated:
                    await self._invalidate_room_threads(
                        room_id,
                        reason="sync_thread_lookup_unavailable",
                    )
                    room_threads_invalidated = True
                continue
            assert impact.thread_id is not None
            await self._invalidate_known_thread(
                room_id,
                impact.thread_id,
                reason="sync_thread_mutation",
            )
            appended = await self._append_event_to_cache(
                room_id,
                impact.thread_id,
                event_source,
                context="sync",
            )
            if not appended:
                await self._invalidate_known_thread(
                    room_id,
                    impact.thread_id,
                    reason="sync_append_failed",
                )

    async def _apply_sync_redactions(
        self,
        room_id: str,
        redacted_event_ids: Sequence[str],
        *,
        resolution_context: _MutationResolutionContext,
    ) -> None:
        room_threads_invalidated = False
        for redacted_event_id in redacted_event_ids:
            impact = await self._resolve_redaction_thread_impact(
                room_id,
                redacted_event_id,
                failure_message="Failed to resolve cached thread for sync redaction",
                resolution_context=resolution_context,
            )
            thread_id = impact.thread_id
            redacted = await self._redact_cached_event(
                room_id,
                redacted_event_id,
                thread_id=thread_id,
                failure_message="Failed to apply sync redaction to cache",
            )
            if impact.state is MutationThreadImpactState.UNKNOWN:
                if not room_threads_invalidated:
                    await self._invalidate_room_threads(
                        room_id,
                        reason="sync_redaction_lookup_unavailable",
                    )
                    room_threads_invalidated = True
                continue
            await self._invalidate_after_redaction(
                room_id,
                impact=impact,
                redacted=redacted,
                success_reason="sync_redaction",
                failure_reason="sync_redaction_failed",
                lookup_unavailable_reason="sync_redaction_lookup_unavailable",
            )

    async def _persist_room_sync_timeline_updates(
        self,
        room_id: str,
        plain_events: Sequence[dict[str, object]],
        threaded_events: Sequence[dict[str, object]],
        redacted_event_ids: Sequence[str],
    ) -> None:
        event_cache = self.runtime.event_cache
        plain_batch = [
            (event_id, room_id, event_source)
            for event_source in plain_events
            if isinstance((event_id := event_source.get("event_id")), str) and event_id
        ]
        threaded_batch = [
            (event_id, room_id, event_source)
            for event_source in threaded_events
            if isinstance((event_id := event_source.get("event_id")), str) and event_id
        ]
        try:
            if plain_batch:
                await event_cache.store_events_batch(plain_batch)
        except Exception as exc:
            self.logger.warning(
                "Failed to persist sync events to cache",
                room_id=room_id,
                event_count=len(plain_batch),
                error=str(exc),
            )
        try:
            if threaded_batch:
                await event_cache.store_events_batch(threaded_batch)
        except Exception as exc:
            self.logger.warning(
                "Failed to persist sync threaded events to cache",
                room_id=room_id,
                event_count=len(threaded_batch),
                error=str(exc),
            )
        resolution_context = await self._build_sync_mutation_resolution_context(
            room_id,
            plain_events=plain_events,
            threaded_events=threaded_events,
        )
        await self._persist_threaded_sync_events(
            room_id,
            threaded_events,
            resolution_context=resolution_context,
        )
        await self._apply_sync_redactions(
            room_id,
            redacted_event_ids,
            resolution_context=resolution_context,
        )

    def _group_sync_timeline_updates(
        self,
        response: nio.SyncResponse,
    ) -> tuple[
        dict[str, list[dict[str, object]]],
        dict[str, list[dict[str, object]]],
        dict[str, list[str]],
    ]:
        room_threaded_events: dict[str, list[dict[str, object]]] = {}
        room_plain_events: dict[str, list[dict[str, object]]] = {}
        room_redactions: dict[str, list[str]] = {}

        joined_rooms = response.rooms.join if isinstance(response.rooms.join, dict) else {}
        for room_id, room_info in joined_rooms.items():
            timeline = room_info.timeline if room_info is not None else None
            events = timeline.events if timeline is not None else ()
            if not isinstance(events, list):
                continue
            for event in events:
                _collect_sync_timeline_cache_updates(
                    room_id,
                    event,
                    room_threaded_events=room_threaded_events,
                    room_plain_events=room_plain_events,
                    room_redactions=room_redactions,
                )
        return room_plain_events, room_threaded_events, room_redactions

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        if not self._cache_runtime_available():
            return
        room_plain_events, room_threaded_events, room_redactions = self._group_sync_timeline_updates(response)
        for room_id in set(room_plain_events) | set(room_threaded_events) | set(room_redactions):
            plain_events = room_plain_events.get(room_id, ())
            threaded_events = room_threaded_events.get(room_id, ())
            redacted_event_ids = room_redactions.get(room_id, ())
            self._queue_room_cache_update(
                room_id,
                lambda room_id=room_id,
                plain_events=plain_events,
                threaded_events=threaded_events,
                redacted_event_ids=redacted_event_ids: self._persist_room_sync_timeline_updates(
                    room_id,
                    plain_events,
                    threaded_events,
                    redacted_event_ids,
                ),
                name="matrix_cache_sync_timeline",
            )

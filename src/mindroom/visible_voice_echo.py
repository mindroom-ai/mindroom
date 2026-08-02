"""Own the router's visible voice-placeholder lifecycle."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any

from mindroom.authorization import is_sender_allowed_for_agent_reply
from mindroom.background_tasks import create_background_task
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.delivery_gateway import EditTextRequest, SendTextRequest
from mindroom.dispatch_handoff import PreparedTextEvent, payload_metadata_from_source
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.matrix.client_thread_history import find_response_event_ids_via_room_messages
from mindroom.turn_origin import original_sender_for_router_relay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.ingress_validation import IngressValidator
    from mindroom.message_target import MessageTarget
    from mindroom.turn_store import TurnStore


_VOICE_TRANSCRIPTION_PLACEHOLDER = "Router agent is transcribing…"

# How long a responder waits for a ready router to enter its echo lifecycle
# before abandoning the turn. This only covers per-bot sync skew: inactive
# routers skip the barrier, while an active router that misses the grace cannot
# let a response overtake a later echo.
_ECHO_CLAIM_GRACE_SECONDS = 0.25
_MAX_SETTLED_ECHO_BARRIERS = 128

type _BarrierKey = tuple[str, str, str]


@dataclass
class _EchoBarrier:
    """Cross-entity publication gate for one raw voice event."""

    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    published_event_id: str | None = None
    settling: bool = False


_echo_barriers: OrderedDict[_BarrierKey, _EchoBarrier] = OrderedDict()


def _reset_visible_voice_echo_barriers() -> None:
    """Drop process-global echo-ordering state so isolated runs cannot inherit it."""
    _echo_barriers.clear()


def _trim_echo_barriers() -> None:
    """Bound terminal history without charging active generations to its budget."""
    settled_keys = [key for key, barrier in _echo_barriers.items() if barrier.settled.is_set()]
    for key in settled_keys[:-_MAX_SETTLED_ECHO_BARRIERS]:
        _echo_barriers.pop(key)


def _echo_barrier(key: _BarrierKey) -> _EchoBarrier:
    barrier = _echo_barriers.get(key)
    if barrier is None:
        barrier = _EchoBarrier()
        _echo_barriers[key] = barrier
    else:
        _echo_barriers.move_to_end(key)
    return barrier


def _claim_echo_barrier(key: _BarrierKey) -> _EchoBarrier:
    """Record that the router owns a visible echo for this audio event."""
    barrier = _echo_barrier(key)
    if barrier.settled.is_set() and barrier.published_event_id is None:
        barrier = _EchoBarrier()
        _echo_barriers[key] = barrier
        _echo_barriers.move_to_end(key)
    barrier.claimed.set()
    return barrier


def _publish_echo_barrier(barrier: _EchoBarrier, event_id: str) -> None:
    """Release waiters once the visible echo exists in the room."""
    if barrier.settled.is_set():
        return
    barrier.published_event_id = event_id
    barrier.claimed.set()
    barrier.settled.set()
    _trim_echo_barriers()


def _fail_echo_barrier(barrier: _EchoBarrier) -> None:
    """Release waiters with no published echo so they abandon their turn."""
    if barrier.settled.is_set():
        return
    barrier.claimed.set()
    barrier.settled.set()
    _trim_echo_barriers()


def _settle_echo_barrier_from_task(barrier: _EchoBarrier, task: asyncio.Task[str | None]) -> None:
    """Settle the barrier from one finished settle task, however that task ended."""
    if task.cancelled():
        _fail_echo_barrier(barrier)
        return
    event_id = None if task.exception() is not None else task.result()
    if event_id is None:
        _fail_echo_barrier(barrier)
    else:
        _publish_echo_barrier(barrier, event_id)


@dataclass
class _UpdateLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    borrowers: int = 0


_update_locks: dict[tuple[str, str, str], _UpdateLockEntry] = {}
_update_locks_guard = Lock()


def _borrow_update_lock(key: tuple[str, str, str]) -> _UpdateLockEntry:
    with _update_locks_guard:
        entry = _update_locks.get(key)
        if entry is None:
            entry = _UpdateLockEntry()
            _update_locks[key] = entry
        entry.borrowers += 1
        return entry


def _release_update_lock(key: tuple[str, str, str], entry: _UpdateLockEntry) -> None:
    with _update_locks_guard:
        entry.borrowers -= 1
        if entry.borrowers == 0 and _update_locks.get(key) is entry:
            _update_locks.pop(key)


@asynccontextmanager
async def _serialize_update(key: tuple[str, str, str]) -> AsyncIterator[None]:
    entry = _borrow_update_lock(key)
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        _release_update_lock(key, entry)


@dataclass(frozen=True)
class VisibleVoiceEchoRequest:
    """Immutable raw-ingress facts needed for one visible voice echo."""

    source_event_id: str
    target: MessageTarget
    requester_user_id: str
    raw_source: dict[str, Any]


@dataclass(frozen=True)
class _VisibleVoiceEchoHandle:
    """One enabled visible-echo lifecycle and its optional placeholder send."""

    request: VisibleVoiceEchoRequest
    barrier: _EchoBarrier
    placeholder_task: asyncio.Task[str | None] | None


@dataclass(frozen=True)
class VisibleVoiceEchoDeps:
    """Collaborators needed for visible voice delivery and durable deduplication."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    agent_name: str
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    ingress: IngressValidator


@dataclass
class VisibleVoiceEchoLifecycle:
    """Post one early router placeholder and settle it exactly once."""

    deps: VisibleVoiceEchoDeps
    _placeholder_tasks: dict[str, asyncio.Task[str | None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def start(self, request: VisibleVoiceEchoRequest) -> _VisibleVoiceEchoHandle | None:
        """Start the earliest truthful visible state for one raw voice event."""
        config = self.deps.runtime.config.voice
        if self.deps.agent_name != ROUTER_AGENT_NAME or not config.visible_router_echo:
            return None
        barrier = _claim_echo_barrier(self._barrier_key(request.target.room_id, request.source_event_id))
        placeholder_task = self._start_placeholder(request, barrier) if config.enabled else None
        return _VisibleVoiceEchoHandle(
            request=request,
            barrier=barrier,
            placeholder_task=placeholder_task,
        )

    async def await_publication(
        self,
        *,
        room: nio.MatrixRoom,
        source_event_id: str,
        requester_user_id: str,
    ) -> bool:
        """Return whether this turn may answer, holding it until the router echo publishes.

        Waiting is unbounded once the router has claimed the echo: the claim is
        always settled, including when the claiming task is cancelled.
        """
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            return True
        key = self._barrier_key(room.room_id, source_event_id)
        barrier = _echo_barriers.get(key)
        if barrier is not None:
            _echo_barriers.move_to_end(key)
        elif self.deps.turn_store.visible_echo_for_source(source_event_id) is not None:
            return True
        if (barrier is None or not barrier.claimed.is_set()) and not self._router_echo_expected(
            room,
            requester_user_id,
        ):
            return True
        if barrier is None:
            barrier = _echo_barrier(key)
        if not barrier.claimed.is_set():
            try:
                await asyncio.wait_for(barrier.claimed.wait(), _ECHO_CLAIM_GRACE_SECONDS)
            except TimeoutError:
                self.deps.logger.warning(
                    "No visible voice echo claimed; abandoning this voice turn",
                    event_id=source_event_id,
                    room_id=room.room_id,
                    grace_seconds=_ECHO_CLAIM_GRACE_SECONDS,
                )
                return False
        await barrier.settled.wait()
        if barrier.published_event_id is None:
            self.deps.logger.warning(
                "Visible voice echo never published; abandoning this voice turn",
                event_id=source_event_id,
                room_id=room.room_id,
            )
            return False
        return True

    def _router_echo_expected(self, room: nio.MatrixRoom, requester_user_id: str) -> bool:
        """Return whether a router in this room will publish a visible echo for this sender."""
        config = self.deps.runtime.config
        if not config.voice.visible_router_echo:
            return False
        orchestrator = self.deps.runtime.orchestrator
        if orchestrator is None or orchestrator.entity_first_sync_complete(ROUTER_AGENT_NAME) is not True:
            return False
        if not any(
            self.deps.ingress.managed_entity_name_for_sender(user_id) == ROUTER_AGENT_NAME for user_id in room.users
        ):
            return False
        return is_sender_allowed_for_agent_reply(
            requester_user_id,
            ROUTER_AGENT_NAME,
            config,
            self.deps.runtime.runtime_paths,
        )

    async def finish(
        self,
        handle: _VisibleVoiceEchoHandle | None,
        normalized_event: PreparedTextEvent,
    ) -> None:
        """Best-effort settle one started lifecycle without blocking canonical dispatch."""
        if handle is None:
            return
        task = self._spawn_settle(
            handle,
            normalized_event,
            name=(f"voice_placeholder_finish:{handle.request.target.room_id}:{handle.request.source_event_id}"),
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.deps.logger.warning(
                "Visible voice echo failed; continuing canonical voice dispatch",
                event_id=handle.request.source_event_id,
                room_id=handle.request.target.room_id,
                exception_type=exc.__class__.__name__,
                error=str(exc),
            )

    def finish_after_cancellation(
        self,
        handle: _VisibleVoiceEchoHandle | None,
        fallback_event: PreparedTextEvent,
    ) -> None:
        """Schedule terminal fallback cleanup without swallowing caller cancellation."""
        if handle is None:
            return
        self._spawn_settle(
            handle,
            fallback_event,
            name=(f"voice_placeholder_cancel_finish:{handle.request.target.room_id}:{handle.request.source_event_id}"),
        )

    def abandon_unsettled(self, handle: _VisibleVoiceEchoHandle | None) -> None:
        """Release waiters when a claimed echo ends without ever attempting to settle."""
        if handle is None:
            return
        if handle.barrier.settling:
            return
        _fail_echo_barrier(handle.barrier)

    def _spawn_settle(
        self,
        handle: _VisibleVoiceEchoHandle,
        event: PreparedTextEvent,
        *,
        name: str,
    ) -> asyncio.Task[str | None]:
        """Run one settle attempt that always settles the ordering barrier when it ends."""
        handle.barrier.settling = True
        task = create_background_task(
            self._settle(handle, event),
            name=name,
            owner=self.deps.runtime,
        )
        task.add_done_callback(
            lambda done_task: _settle_echo_barrier_from_task(
                handle.barrier,
                done_task,
            ),
        )
        return task

    def _start_placeholder(
        self,
        request: VisibleVoiceEchoRequest,
        barrier: _EchoBarrier,
    ) -> asyncio.Task[str | None]:
        existing_task = self._placeholder_tasks.get(request.source_event_id)
        if existing_task is not None:
            return existing_task
        task = create_background_task(
            self._send_placeholder(request, barrier),
            name=f"voice_placeholder:{request.target.room_id}:{request.source_event_id}",
            owner=self.deps.runtime,
        )
        self._placeholder_tasks[request.source_event_id] = task
        task.add_done_callback(
            lambda completed_task: self._clear_placeholder_task(
                request.source_event_id,
                completed_task,
            ),
        )
        return task

    def _clear_placeholder_task(
        self,
        source_event_id: str,
        completed_task: asyncio.Task[str | None],
    ) -> None:
        if self._placeholder_tasks.get(source_event_id) is completed_task:
            self._placeholder_tasks.pop(source_event_id)

    async def _send_placeholder(
        self,
        request: VisibleVoiceEchoRequest,
        barrier: _EchoBarrier,
    ) -> str | None:
        async with _serialize_update(self._update_key(request)):
            existing_event_id = self.deps.turn_store.visible_echo_for_source(request.source_event_id)
            if existing_event_id is not None:
                _publish_echo_barrier(barrier, existing_event_id)
                return existing_event_id
            existing_event_id = await self._recover_visible_echo_event_id(request)
            if existing_event_id is not None:
                self.deps.turn_store.record_visible_echo(request.source_event_id, existing_event_id)
                _publish_echo_barrier(barrier, existing_event_id)
                return existing_event_id
            event_id = await self.deps.delivery_gateway.send_text(
                SendTextRequest(
                    target=request.target,
                    response_text=_VOICE_TRANSCRIPTION_PLACEHOLDER,
                    skip_mentions=True,
                    extra_content=self._extra_content(
                        requester_user_id=request.requester_user_id,
                        normalized_source=request.raw_source,
                    ),
                ),
            )
            if event_id is not None:
                self.deps.turn_store.record_visible_echo(request.source_event_id, event_id)
                _publish_echo_barrier(barrier, event_id)
            return event_id

    async def _settle(
        self,
        handle: _VisibleVoiceEchoHandle,
        normalized_event: PreparedTextEvent,
    ) -> str | None:
        request = handle.request
        is_fallback = _is_raw_audio_fallback(normalized_event)
        placeholder_event_id = (
            await asyncio.shield(handle.placeholder_task) if handle.placeholder_task is not None else None
        )
        async with _serialize_update(self._update_key(request)):
            finalized = self.deps.turn_store.finalized_visible_echo(request.source_event_id)
            if finalized is not None and (not finalized.is_fallback or is_fallback):
                return finalized.event_id

            event_id = placeholder_event_id or self.deps.turn_store.visible_echo_for_source(
                request.source_event_id,
            )
            extra_content = self._extra_content(
                requester_user_id=request.requester_user_id,
                normalized_source=normalized_event.source,
            )
            if event_id is None:
                event_id = await self._recover_visible_echo_event_id(request)
                if event_id is not None:
                    self.deps.turn_store.record_visible_echo(request.source_event_id, event_id)
            if event_id is None:
                event_id = await self.deps.delivery_gateway.send_text(
                    SendTextRequest(
                        target=request.target,
                        response_text=normalized_event.body,
                        skip_mentions=True,
                        extra_content=extra_content,
                    ),
                )
                if event_id is None:
                    return None
                self.deps.turn_store.record_visible_echo(request.source_event_id, event_id)
            else:
                edited = await self.deps.delivery_gateway.edit_text(
                    EditTextRequest(
                        target=request.target,
                        event_id=event_id,
                        new_text=normalized_event.body,
                        extra_content=extra_content,
                        retry_sync_recovery=True,
                    ),
                )
                if not edited:
                    return None

            self.deps.turn_store.record_finalized_visible_echo(
                request.source_event_id,
                event_id,
                is_fallback=is_fallback,
            )
            return event_id

    async def _recover_visible_echo_event_id(self, request: VisibleVoiceEchoRequest) -> str | None:
        """Adopt one untracked marked echo before replay can send another."""
        if not turn_dispatch_recovery_active():
            return None
        client = self.deps.runtime.client
        if client is None or client.user_id is None:
            msg = "Visible voice echo recovery requires an active Matrix client"
            raise RuntimeError(msg)
        response_event_ids = await find_response_event_ids_via_room_messages(
            client,
            request.target.room_id,
            response_sender=client.user_id,
            source_event_ids=(request.source_event_id,),
            response_source_filter=lambda source: (
                isinstance(content := source.get("content"), dict)
                and content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is True
            ),
        )
        if len(response_event_ids) > 1:
            msg = "Recovered voice source has multiple visible router echoes"
            raise RuntimeError(msg)
        return next(iter(response_event_ids), None)

    def _update_key(self, request: VisibleVoiceEchoRequest) -> tuple[str, str, str]:
        return (self.deps.agent_name, request.target.room_id, request.source_event_id)

    def _barrier_key(self, room_id: str, source_event_id: str) -> _BarrierKey:
        """Key one echo barrier so every entity in this process shares it."""
        return (str(self.deps.runtime.runtime_paths.storage_root.resolve()), room_id, source_event_id)

    def _extra_content(
        self,
        *,
        requester_user_id: str,
        normalized_source: dict[str, Any],
    ) -> dict[str, Any]:
        payload_metadata = payload_metadata_from_source(normalized_source, trust_internal_metadata=True)
        inherited_original_sender = payload_metadata.original_sender
        relay_original_sender = original_sender_for_router_relay(
            requester_id=requester_user_id,
            requester_entity_name=self.deps.ingress.managed_entity_name_for_sender(requester_user_id),
            inherited_original_sender=inherited_original_sender,
            inherited_original_sender_entity_name=(
                self.deps.ingress.managed_entity_name_for_sender(inherited_original_sender)
                if inherited_original_sender is not None
                else None
            ),
        )
        extra_content: dict[str, Any] = {
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        }
        if relay_original_sender is not None:
            extra_content[ORIGINAL_SENDER_KEY] = relay_original_sender
        if payload_metadata.attachment_ids:
            extra_content[ATTACHMENT_IDS_KEY] = list(payload_metadata.attachment_ids)
        if payload_metadata.raw_audio_fallback:
            extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
        if payload_metadata.voice_transcript:
            extra_content[VOICE_TRANSCRIPT_KEY] = True
        return extra_content


def _is_raw_audio_fallback(event: PreparedTextEvent) -> bool:
    content = event.source.get("content")
    return isinstance(content, dict) and content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True

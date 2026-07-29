"""Own the router's visible voice-placeholder lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any

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
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.turn_origin import original_sender_for_router_relay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.ingress_validation import IngressValidator
    from mindroom.message_target import MessageTarget
    from mindroom.turn_store import TurnStore


_VOICE_TRANSCRIPTION_PLACEHOLDER = "Router agent is transcribing…"


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
        placeholder_task = self._start_placeholder(request) if config.enabled else None
        return _VisibleVoiceEchoHandle(request=request, placeholder_task=placeholder_task)

    async def finish(
        self,
        handle: _VisibleVoiceEchoHandle | None,
        normalized_event: PreparedTextEvent,
    ) -> None:
        """Best-effort settle one started lifecycle without blocking canonical dispatch."""
        if handle is None:
            return
        task = create_background_task(
            self._settle(handle, normalized_event),
            name=(f"voice_placeholder_finish:{handle.request.target.room_id}:{handle.request.source_event_id}"),
            owner=self.deps.runtime,
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
        create_background_task(
            self._settle(handle, fallback_event),
            name=(f"voice_placeholder_cancel_finish:{handle.request.target.room_id}:{handle.request.source_event_id}"),
            owner=self.deps.runtime,
        )

    def _start_placeholder(self, request: VisibleVoiceEchoRequest) -> asyncio.Task[str | None]:
        existing_task = self._placeholder_tasks.get(request.source_event_id)
        if existing_task is not None:
            return existing_task
        task = create_background_task(
            self._send_placeholder(request),
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

    async def _send_placeholder(self, request: VisibleVoiceEchoRequest) -> str | None:
        async with _serialize_update(self._update_key(request)):
            existing_event_id = self.deps.turn_store.visible_echo_for_source(request.source_event_id)
            if existing_event_id is not None:
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

    def _update_key(self, request: VisibleVoiceEchoRequest) -> tuple[str, str, str]:
        return (self.deps.agent_name, request.target.room_id, request.source_event_id)

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

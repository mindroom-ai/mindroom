"""Voice preparation and transcript failures must preserve the exact durable reply obligation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom import pending_event_worker
from mindroom.coalescing.gate import CoalescingGate
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY, VOICE_TRANSCRIPT_KEY
from mindroom.conversation_resolver import ConversationResolver
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind, TurnRecordStore
from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, _VoiceNormalizationResult
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.journal_helpers import admit_dispatch_event
from tests.test_turns_focused import (
    _ROOM_ID,
    _SENDER,
    _THREAD_ROOT,
    _build_harness,
    _entity_user_id,
    _obligation_runner,
    _replace_harness_deps,
    _room_with_members,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.hooks import MessageEnvelope
    from mindroom.journal_dispatch import JournalDispatcher
    from tests.test_turns_focused import _Harness


@pytest.fixture(autouse=True)
def _short_room_retry_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each room retry's cooldown and ordering without sleeping a real second per failure."""
    monkeypatch.setattr(pending_event_worker, "_INITIAL_RETRY_DELAY_SECONDS", 0.01)


def _wire_dispatcher(harness: _Harness, room: nio.MatrixRoom, tmp_path: Path) -> JournalDispatcher:
    """Use production lane-to-journal handoffs with the focused controller fixture."""
    dispatcher = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch",
        matrix_user_id=harness.ingress.deps.matrix_id.full_id,
        entity_name=harness.ingress.deps.agent_name,
        room=room,
    )

    async def settle_ignored(source_event_ids: tuple[str, ...]) -> None:
        harness.ignored_dispatch_sources.append(source_event_ids)
        await dispatcher.settle_intentionally_ignored_turn_sources(source_event_ids)

    def retry(room_id: str, source_event_id: str) -> None:
        harness.retried_dispatch_sources.append((source_event_id,))
        dispatcher.retry_turn_source(room_id, source_event_id)

    gate = CoalescingGate(
        dispatch_turn=harness.dispatcher.handle_prepared_turn,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        on_undelivered_source=retry,
        on_intentionally_ignored_sources=settle_ignored,
    )
    harness.gate = gate
    _replace_harness_deps(
        harness,
        coalescing_gate=gate,
        retry_dispatch_sources=dispatcher.retry_turn_sources,
        settle_dispatch_sources=dispatcher.settle_intentionally_ignored_turn_sources,
        dispatch_source_is_terminal=dispatcher.source_is_terminal,
        visible_responses=replace(
            harness.dispatcher.deps.visible_responses,
            deps=replace(
                harness.dispatcher.deps.visible_responses.deps,
                settle_ignored_sources=dispatcher.settle_intentionally_ignored_turn_sources,
            ),
        ),
    )
    dispatcher.callbacks = replace(dispatcher.callbacks, source_has_live_owner=gate.has_pending_source_event)
    return dispatcher


def _force_readiness_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail input preparation so the normalizer supplies a raw-audio fallback."""
    monkeypatch.setattr(
        InboundTurnNormalizer,
        "prepare_voice_event",
        AsyncMock(side_effect=RuntimeError("Transcription unavailable")),
    )


def _voice_event(
    config: Config,
    mentions: tuple[str, ...] = ("general",),
) -> tuple[nio.RoomMessageAudio, PreparedIngress]:
    """One threaded audio source and its successful transcription."""
    event = cast(
        "nio.RoomMessageAudio",
        nio.RoomMessageAudio.from_dict(
            {
                "event_id": "$delayed-voice",
                "sender": _SENDER,
                "origin_server_ts": 1_000_000,
                "type": "m.room.message",
                "room_id": _ROOM_ID,
                "content": {
                    "msgtype": "m.audio",
                    "body": "voice.ogg",
                    "url": "mxc://localhost/voice",
                    "m.mentions": {"user_ids": [_entity_user_id(config, name) for name in mentions]},
                    "m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD_ROOT},
                },
            },
        ),
    )
    normalized = PreparedIngress(
        sender=event.sender,
        event_id=event.event_id,
        body="Please review the new PR",
        source={
            **event.source,
            "content": {
                **event.source["content"],
                "msgtype": "m.text",
                "body": "Please review the new PR",
                SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                ORIGINAL_SENDER_KEY: event.sender,
                VOICE_TRANSCRIPT_KEY: True,
            },
        },
        server_timestamp=event.server_timestamp,
        payload_trusted=True,
    )
    return event, normalized


@pytest.mark.asyncio
async def test_prepared_voice_write_failure_preserves_pending_source_without_raw_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed checkpoint releases ownership without silently replacing the transcript."""
    config = bind_runtime_paths(
        Config(agents={"general": {"display_name": "General"}}, voice={"visible_router_echo": True}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event, normalized = _voice_event(config)
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    fallback = AsyncMock(side_effect=AssertionError("Checkpoint failure must not re-normalize"))
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize)
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_raw_voice_fallback_event", fallback)
    monkeypatch.setattr(TurnRecordStore, "write", AsyncMock(side_effect=RuntimeError("Journal unavailable")))
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.runner.requests == []
        assert harness.ignored_dispatch_sources == []
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        assert not harness.turn_store.has_live_turn_claim(event.event_id)
        assert not harness.gate.has_pending_source_event(event.event_id)
        assert await harness.turn_store.prepared_voice_for_source(event.event_id) is None
        normalize.assert_awaited_once()
        fallback.assert_not_awaited()
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()


@pytest.mark.asyncio
async def test_voice_metadata_failure_retries_without_retranscription(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Envelope failure after STT must retain the prepared source, not dispatch an incomplete fallback."""
    config = bind_runtime_paths(
        Config(agents={"general": {"display_name": "General"}}, voice={"visible_router_echo": False}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event, normalized = _voice_event(config)
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize)
    build_envelope = ConversationResolver.build_envelope
    fail_metadata = True

    def build_or_fail(self: ConversationResolver, **kwargs: Any) -> MessageEnvelope:  # noqa: ANN401
        if fail_metadata and isinstance(kwargs["event"], PreparedIngress):
            msg = "Voice dispatch metadata unavailable"
            raise RuntimeError(msg)
        return build_envelope(self, **kwargs)

    monkeypatch.setattr(ConversationResolver, "build_envelope", build_or_fail)
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.runner.requests == []
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        assert not harness.turn_store.has_live_turn_claim(event.event_id)
        assert (await harness.turn_store.prepared_voice_for_source(event.event_id)).body == "Please review the new PR"
        fail_metadata = False
        config.agents["general"].thread_mode = "room"
        normalize.side_effect = AssertionError("Metadata retry must reuse the checkpoint")
        dispatcher.release_turn_replay()
        dispatcher.start()
        await asyncio.wait_for(harness.runner.response_started.wait(), timeout=5)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert [request.prompt for request in harness.runner.requests] == ["Please review the new PR"]
        assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)
        normalize.assert_awaited_once()
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("send_raises", [False, True])
async def test_failed_transcript_send_retries_from_the_checkpoint_and_posts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
    send_raises: bool,
) -> None:
    """A transcript that could not be sent leaves the voice message pending; its retry posts it once, then replies."""
    config = bind_runtime_paths(
        Config(agents={"general": {"display_name": "General"}}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    await harness.turn_store.warm()
    room = _room_with_members(config, "general")
    event, normalized = _voice_event(config)
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize)
    send = harness.gateway.send_text
    failed_send = (
        AsyncMock(side_effect=RuntimeError("Matrix unavailable")) if send_raises else AsyncMock(return_value=None)
    )
    monkeypatch.setattr(harness.gateway, "send_text", failed_send)
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.runner.requests == []
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        assert not harness.turn_store.has_live_turn_claim(event.event_id)
        snapshot = await harness.turn_store.prepared_voice_for_source(event.event_id)
        assert snapshot is not None
        assert snapshot.to_content() == normalized.source["content"]
        normalize.side_effect = AssertionError("Retry must reuse the checkpoint")
        if restart:
            await dispatcher.stop()
            harness = _build_harness(config, tmp_path)
            await harness.turn_store.warm()
            dispatcher = _wire_dispatcher(harness, room, tmp_path)
        else:
            monkeypatch.setattr(harness.gateway, "send_text", send)

        dispatcher.release_turn_replay()
        dispatcher.start()
        await asyncio.wait_for(harness.runner.response_started.wait(), timeout=5)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert [request.response_text for request in harness.gateway.sent] == ["Please review the new PR"]
        assert (await harness.turn_store.visible_echo(event.event_id)).event_id == "$sent-1:localhost"
        assert [request.prompt for request in harness.runner.requests] == ["Please review the new PR"]
        normalize.assert_awaited_once()
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()

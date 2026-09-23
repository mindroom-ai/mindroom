"""Voice publication delays must preserve the exact durable reply obligation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.coalescing import CoalescingGate
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, SOURCE_KIND_KEY
from mindroom.conversation_resolver import ConversationResolver
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind, TurnRecordStore
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, _VoiceNormalizationResult
from mindroom.visible_voice_echo import (
    VisibleVoiceEchoLifecycle,
    VisibleVoiceEchoRequest,
    _reset_visible_voice_echo_barriers,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.journal_helpers import admit_dispatch_event
from tests.test_turn_controller_focused import (
    _ROOM_ID,
    _SENDER,
    _THREAD_ROOT,
    _build_harness,
    _entity_user_id,
    _obligation_runner,
    _room_with_members,
    _settle_dispatcher,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.hooks import MessageEnvelope
    from mindroom.journal_dispatch import JournalDispatcher
    from tests.test_turn_controller_focused import _Harness


def _wire_dispatcher(harness: _Harness, room: nio.MatrixRoom, tmp_path: Path) -> JournalDispatcher:
    """Use production lane-to-journal handoffs with the focused controller fixture."""
    dispatcher = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch",
        principal_id=harness.controller.deps.matrix_id.full_id,
        entity_name=harness.controller.deps.agent_name,
        room=room,
    )

    async def settle_ignored(source_event_id: str) -> None:
        harness.ignored_dispatch_sources.append((source_event_id,))
        await dispatcher.settle_intentionally_ignored_turn_sources((source_event_id,))

    def retry(room_id: str, source_event_id: str) -> None:
        harness.retried_dispatch_sources.append((source_event_id,))
        dispatcher.retry_turn_source(room_id, source_event_id)

    gate = CoalescingGate(
        dispatch_turn=harness.controller.handle_prepared_turn,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        on_undelivered_source=retry,
        on_intentionally_ignored_source=settle_ignored,
    )
    harness.gate = gate
    harness.controller.deps = replace(
        harness.controller.deps,
        coalescing_gate=gate,
        retry_dispatch_sources=dispatcher.retry_turn_sources,
        settle_dispatch_sources=dispatcher.settle_intentionally_ignored_turn_sources,
        dispatch_source_is_terminal=dispatcher.source_is_terminal,
        visible_responses=replace(
            harness.controller.deps.visible_responses,
            deps=replace(
                harness.controller.deps.visible_responses.deps,
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


def _voice_event(config: Config) -> tuple[nio.RoomMessageAudio, PreparedIngress]:
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
                    "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
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
            },
        },
        server_timestamp=event.server_timestamp,
        source_kind_override=VOICE_SOURCE_KIND,
    )
    return event, normalized


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("restart_after_echo", [False, True])
@pytest.mark.parametrize("readiness_fallback", [False, True])
async def test_delayed_voice_echo_keeps_source_pending_until_one_reply(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart_after_echo: bool,
    readiness_fallback: bool,
) -> None:
    """Claim timeout must neither settle the audio nor lose a published echo on restart."""
    config = bind_runtime_paths(
        Config(agents={"general": {"display_name": "General"}}, voice={"visible_router_echo": True}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    router = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    await harness.turn_store.warm()
    await router.turn_store.warm()
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event, normalized = _voice_event(config)
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    monkeypatch.setattr(
        InboundTurnNormalizer,
        "prepare_raw_voice_fallback_event" if readiness_fallback else "prepare_voice_event",
        normalize,
    )
    if readiness_fallback:
        _force_readiness_fallback(monkeypatch)
    monkeypatch.setattr(VisibleVoiceEchoLifecycle, "_router_echo_expected", lambda *_: True)
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()

        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.ignored_dispatch_sources == []
        assert harness.runner.requests == []
        assert not harness.turn_store.has_live_turn_claim(event.event_id)
        assert not harness.gate.has_pending_source_event(event.event_id)
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        normalize.assert_awaited_once()
        normalize.side_effect = RuntimeError("Provider unavailable after deferral")
        snapshot = harness.turn_store.prepared_voice_for_source(event.event_id)
        assert snapshot is not None
        assert snapshot.body == "Please review the new PR"
        assert snapshot.to_content() == normalized.source["content"]
        assert snapshot.thread_id == _THREAD_ROOT

        target = router.controller.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=_THREAD_ROOT,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        echo = router.controller.deps.visible_voice_echo
        handle = echo.start(
            VisibleVoiceEchoRequest(
                source_event_id=event.event_id,
                target=target,
                requester_user_id=event.sender,
                raw_source=event.source,
            ),
        )
        assert handle is not None
        await echo.finish(handle, normalized)
        assert len(router.gateway.sent) == 1

        if restart_after_echo:
            await dispatcher.stop()
            _reset_visible_voice_echo_barriers()
            _reset_handled_turn_ledger_runtime()
            harness = _build_harness(config, tmp_path)
            await harness.turn_store.warm()
            assert harness.turn_store.prepared_voice_for_source(event.event_id) == snapshot
            dispatcher = _wire_dispatcher(harness, room, tmp_path)

        # The journal's retry timer owns recovery: no new Matrix event is admitted.
        dispatcher.release_turn_replay()
        dispatcher.start()
        await asyncio.wait_for(harness.runner.response_started.wait(), timeout=5)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert [request.prompt for request in harness.runner.requests] == ["Please review the new PR"]
        assert harness.turn_store.is_handled(event.event_id)
        assert harness.turn_store.prepared_voice_for_source(event.event_id) is None
        normalize.assert_awaited_once()

        await harness.controller.handle_media_event(room, event)
        await harness.gate.drain_all()
        assert len(harness.runner.requests) == 1
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()


@pytest.mark.asyncio
async def test_echo_journal_read_failure_reuses_prepared_transcription(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An echo lookup failure must preserve completed STT for the journal retry."""
    config = bind_runtime_paths(
        Config(agents={"general": {"display_name": "General"}}, voice={"visible_router_echo": True}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event, normalized = _voice_event(config)
    router_record = replace(TurnRecord.create([event.event_id], completed=False), visible_echo_event_id="$echo")
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    monkeypatch.setattr(
        InboundTurnNormalizer,
        "prepare_voice_event",
        normalize,
    )
    monkeypatch.setattr(
        TurnRecordStore,
        "load",
        AsyncMock(side_effect=[RuntimeError("Temporary journal read failure"), router_record]),
    )
    monkeypatch.setattr(VisibleVoiceEchoLifecycle, "_router_echo_expected", lambda *_: True)
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()

        assert harness.runner.requests == []
        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        assert not harness.turn_store.has_live_turn_claim(event.event_id)
        normalize.assert_awaited_once()
        normalize.side_effect = RuntimeError("Provider unavailable after deferral")

        dispatcher.release_turn_replay()
        dispatcher.start()
        await asyncio.wait_for(harness.runner.response_started.wait(), timeout=5)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert [request.prompt for request in harness.runner.requests] == ["Please review the new PR"]
        assert harness.turn_store.is_handled(event.event_id)
        assert harness.turn_store.prepared_voice_for_source(event.event_id) is None
        normalize.assert_awaited_once()
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()


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
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event, normalized = _voice_event(config)
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    fallback = AsyncMock(side_effect=AssertionError("Checkpoint failure must not re-normalize"))
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize)
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_raw_voice_fallback_event", fallback)
    monkeypatch.setattr(TurnRecordStore, "upsert", AsyncMock(side_effect=RuntimeError("Journal unavailable")))
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
        assert harness.turn_store.prepared_voice_for_source(event.event_id) is None
        normalize.assert_awaited_once()
        fallback.assert_not_awaited()
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()


@pytest.mark.asyncio
async def test_router_becoming_ready_during_transcription_still_requires_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final applicability must be rechecked after potentially slow transcription."""
    config = bind_runtime_paths(
        Config(agents={"general": {"display_name": "General"}}, voice={"visible_router_echo": True}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event, normalized = _voice_event(config)
    router_ready = False

    async def normalize_once(*_: object) -> _VoiceNormalizationResult:
        nonlocal router_ready
        router_ready = True
        return _VoiceNormalizationResult(event=normalized)

    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize_once)
    monkeypatch.setattr(VisibleVoiceEchoLifecycle, "_router_echo_expected", lambda *_: router_ready)
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        assert router_ready
        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.runner.requests == []
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        assert harness.turn_store.prepared_voice_for_source(event.event_id).body == "Please review the new PR"
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("send_raises", [False, True])
@pytest.mark.parametrize("root_audio", [False, True])
@pytest.mark.parametrize("voice_enabled", [False, True])
async def test_router_initial_echo_failure_retains_owner_and_reuses_checkpoint(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
    send_raises: bool,
    root_audio: bool,
    voice_enabled: bool,
) -> None:
    """A failed initial publication must leave a router callback to replay, even after restart."""
    config = bind_runtime_paths(
        Config(
            agents={"general": {"display_name": "General"}},
            voice={"enabled": voice_enabled, "visible_router_echo": True},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    router = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    await router.turn_store.warm()
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event, normalized = _voice_event(config)
    if root_audio:
        event.source["content"].pop("m.relates_to")
        normalized.source["content"].pop("m.relates_to")
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize)
    send = router.gateway.send_text
    failed_send = (
        AsyncMock(side_effect=RuntimeError("Matrix unavailable")) if send_raises else AsyncMock(return_value=None)
    )
    monkeypatch.setattr(router.gateway, "send_text", failed_send)
    dispatcher = _wire_dispatcher(router, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await router.gate.drain_all()
        assert await dispatcher.store.is_pending(event.event_id)
        assert not router.turn_store.is_handled(event.event_id)
        assert router.ignored_dispatch_sources == []
        assert router.retried_dispatch_sources == [(event.event_id,)]
        assert not router.turn_store.has_live_turn_claim(event.event_id)
        snapshot = router.turn_store.prepared_voice_for_source(event.event_id)
        assert snapshot is not None
        assert snapshot.body == "Please review the new PR"
        assert snapshot.thread_id == (event.event_id if root_audio else _THREAD_ROOT)
        assert snapshot.coalescing_thread_id == (None if root_audio else _THREAD_ROOT)
        assert snapshot.to_content() == normalized.source["content"]
        normalize.side_effect = AssertionError("Retry must reuse the checkpoint")
        if restart:
            await dispatcher.stop()
            _reset_visible_voice_echo_barriers()
            _reset_handled_turn_ledger_runtime()
            router = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
            await router.turn_store.warm()
            assert router.turn_store.prepared_voice_for_source(event.event_id) == snapshot
            dispatcher = _wire_dispatcher(router, room, tmp_path)
        else:
            monkeypatch.setattr(router.gateway, "send_text", send)

        dispatcher.release_turn_replay()
        dispatcher.start()
        await _settle_dispatcher(router, dispatcher)
        await router.gate.drain_all()
        assert len(router.gateway.sent) == 1
        assert router.turn_store.visible_echo_for_source(event.event_id) == "$sent-1:localhost"
        normalize.assert_awaited_once()
        await router.controller.handle_media_event(room, event)
        await router.gate.drain_all()
        assert len(router.gateway.sent) == 1
    finally:
        await dispatcher.stop()
        await router.gate.drain_all()


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
    build_envelope = ConversationResolver.build_ingress_envelope
    fail_metadata = True

    def build_or_fail(self: ConversationResolver, **kwargs: Any) -> MessageEnvelope:  # noqa: ANN401
        if fail_metadata and isinstance(kwargs["event"], PreparedIngress):
            msg = "Voice dispatch metadata unavailable"
            raise RuntimeError(msg)
        return build_envelope(self, **kwargs)

    monkeypatch.setattr(ConversationResolver, "build_ingress_envelope", build_or_fail)
    dispatcher = _wire_dispatcher(harness, room, tmp_path)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        assert await dispatcher.store.is_pending(event.event_id)
        assert harness.runner.requests == []
        assert harness.retried_dispatch_sources == [(event.event_id,)]
        assert not harness.turn_store.has_live_turn_claim(event.event_id)
        assert harness.turn_store.prepared_voice_for_source(event.event_id).body == "Please review the new PR"
        fail_metadata = False
        config.agents["general"].thread_mode = "room"
        normalize.side_effect = AssertionError("Metadata retry must reuse the checkpoint")
        dispatcher.release_turn_replay()
        dispatcher.start()
        await asyncio.wait_for(harness.runner.response_started.wait(), timeout=5)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert [request.prompt for request in harness.runner.requests] == ["Please review the new PR"]
        assert harness.turn_store.is_handled(event.event_id)
        normalize.assert_awaited_once()
    finally:
        await dispatcher.stop()
        await harness.gate.drain_all()

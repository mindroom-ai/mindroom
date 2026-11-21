"""The agent that answers a voice message posts its transcript once, before its reply."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.config.main import Config
from mindroom.config.participation import ParticipationConfig
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTE_EVENT_TYPE,
    ROUTER_AGENT_NAME,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_SOURCE_EVENT_ID_KEY,
)
from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, _VoiceNormalizationResult
from mindroom.routing_judgment import ResponderSelection
from mindroom.turn_policy import DispatchPlan, ResponseAction
from mindroom.voice import visible_voice_echo
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.test_turns_focused import (
    _SENDER,
    _THREAD_ROOT,
    _build_harness,
    _room_with_members,
    _route_as_delivered,
    _text_event,
)
from tests.test_voice_echo_retry import _force_readiness_fallback, _voice_event

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.delivery_gateway import SendTextRequest
    from mindroom.dispatch_handoff import PreparedIngress
    from tests.test_turns_focused import _Harness

_TRANSCRIPT = "Please review the new PR"


def _config(tmp_path: Path, **voice: object) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": {"display_name": "General"}, "research": {"display_name": "Research"}},
            voice=voice,
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )


def _transcribes(monkeypatch: pytest.MonkeyPatch, normalized: PreparedIngress) -> AsyncMock:
    """Serve one successful transcription to every bot's voice preparation."""
    normalize = AsyncMock(return_value=_VoiceNormalizationResult(event=normalized))
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", normalize)
    return normalize


async def _deliver(harness: _Harness, room: nio.MatrixRoom, event: nio.Event) -> None:
    """Run one voice message, or the route of one, until its reply settles."""
    if ROUTE_EVENT_TYPE in event.source:
        await harness.ingress.handle_text_event(room, event)
    else:
        await harness.ingress.handle_media_event(room, event)
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()


def _record_replies_at_send(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many replies had started each time the bot sends a message."""
    replies_at_send: list[int] = []
    send = harness.gateway.send_text

    async def send_text(request: SendTextRequest) -> str | None:
        replies_at_send.append(len(harness.runner.requests))
        return await send(request)

    monkeypatch.setattr(harness.gateway, "send_text", send_text)
    return replies_at_send


@pytest.mark.asyncio
async def test_answering_agent_posts_transcript_before_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript is in the room, as today's echo content, before the reply starts."""
    config = _config(tmp_path)
    harness = _build_harness(config, tmp_path)
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    replies_at_send = _record_replies_at_send(harness, monkeypatch)

    await _deliver(harness, _room_with_members(config, "general", "research"), event)

    [transcript] = harness.gateway.sent
    assert replies_at_send == [0]
    assert transcript.response_text == _TRANSCRIPT
    assert transcript.skip_mentions is True
    assert transcript.target.resolved_thread_id == _THREAD_ROOT
    assert transcript.extra_content is not None
    assert transcript.extra_content[VISIBLE_ROUTER_VOICE_ECHO_KEY] is True
    assert transcript.extra_content[VOICE_SOURCE_EVENT_ID_KEY] == event.event_id
    assert transcript.extra_content[ORIGINAL_SENDER_KEY] == _SENDER
    assert [request.prompt for request in harness.runner.requests] == [_TRANSCRIPT]
    assert await harness.turn_store.visible_echo(event.event_id) == "$sent-1:localhost"
    assert harness.gateway.edited == []  # No placeholder was sent and replaced.


@pytest.mark.parametrize("checkpoint_cleaned", [False, True])
@pytest.mark.asyncio
async def test_routed_responder_posts_transcript_before_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checkpoint_cleaned: bool,
) -> None:
    """A steward that routes a voice message posts nothing; the responder it selects posts the transcript.

    Retention may clear the checkpoint of a voice message its bot ignored; the route still carries the transcript.
    """
    config = _config(tmp_path)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    event, normalized = _voice_event(config, mentions=())
    _transcribes(monkeypatch, normalized)
    monkeypatch.setattr(
        "mindroom.router.relay.suggest_responder_for_message",
        AsyncMock(return_value=ResponderSelection("general")),
    )
    steward = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    responder = _build_harness(config, tmp_path)
    replies_at_send = _record_replies_at_send(responder, monkeypatch)

    await _deliver(steward, room, event)
    await _deliver(responder, room, event)
    assert steward.gateway.sent == responder.gateway.sent == []
    assert responder.runner.requests == []
    if checkpoint_cleaned:
        await responder.turn_store.cleanup(unsettled_source_event_ids=frozenset())
        assert await responder.turn_store.prepared_voice_for_source(event.event_id) is None

    [route] = steward.gateway.routes
    await _deliver(responder, room, _route_as_delivered(config, route, steward=ROUTER_AGENT_NAME))

    [transcript] = responder.gateway.sent
    assert transcript.response_text == _TRANSCRIPT
    assert transcript.target.reply_to_event_id == event.event_id
    assert transcript.extra_content[ORIGINAL_SENDER_KEY] == _SENDER
    assert replies_at_send == [0]
    assert len(responder.runner.requests) == 1


@pytest.mark.asyncio
async def test_no_transcript_when_nobody_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A voice message for another agent gets no transcript from a bot that does not answer it."""
    config = _config(tmp_path)
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)

    await _deliver(harness, _room_with_members(config, ROUTER_AGENT_NAME, "general"), event)

    assert harness.gateway.sent == []
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_restart_after_transcript_does_not_post_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bot that restarts between its transcript and its reply finds its receipt and only replies."""
    config = _config(tmp_path)
    room = _room_with_members(config, "general")
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    crashed = _build_harness(config, tmp_path)
    crashed.runner.pre_lock_error = RuntimeError("crash before the reply")
    with pytest.raises(RuntimeError, match="crash before the reply"):
        await _deliver(crashed, room, event)
    assert len(crashed.gateway.sent) == 1

    restarted = _build_harness(config, tmp_path)
    await _deliver(restarted, room, event)

    assert restarted.gateway.sent == []
    assert [request.prompt for request in restarted.runner.requests] == [_TRANSCRIPT]


@pytest.mark.asyncio
async def test_two_answering_bots_post_one_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Of two bots that answer one voice message, one posts the transcript and the other's reply waits for it."""
    config = _config(tmp_path)
    room = _room_with_members(config, "general", "research")
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    first = _build_harness(config, tmp_path)
    second = _build_harness(config, tmp_path, agent_name="research")
    _answers(second, monkeypatch)
    send_started, release_send = asyncio.Event(), asyncio.Event()
    send = first.gateway.send_text

    async def slow_send(request: SendTextRequest) -> str | None:
        send_started.set()
        await release_send.wait()
        return await send(request)

    monkeypatch.setattr(first.gateway, "send_text", slow_send)
    first_delivery = asyncio.create_task(_deliver(first, room, event))
    await asyncio.wait_for(send_started.wait(), timeout=5)
    second_delivery = asyncio.create_task(_deliver(second, room, event))
    await asyncio.sleep(0.2)
    assert second.runner.requests == []

    release_send.set()
    await asyncio.wait_for(asyncio.gather(first_delivery, second_delivery), timeout=5)

    assert first.retried_dispatch_sources == second.retried_dispatch_sources == []
    assert [request.response_text for request in first.gateway.sent] == [_TRANSCRIPT]
    assert second.gateway.sent == []
    assert len(first.runner.requests) == len(second.runner.requests) == 1


async def _owner_fails_to_post(config: Config, tmp_path: Path, room: nio.MatrixRoom, event: nio.Event) -> None:
    """Let General claim the transcript and fail to send it, as a bot whose Matrix send failed does."""
    owner = _build_harness(config, tmp_path)
    owner.gateway.send_text = AsyncMock(return_value=None)
    await _deliver(owner, room, event)
    assert owner.runner.requests == []


def _answers(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan an individual answer, as each of two bots a person addresses separately does."""
    answer = DispatchPlan(kind="respond", response_action=ResponseAction(kind="individual"))
    monkeypatch.setattr(harness.policy, "plan_turn", AsyncMock(return_value=answer))


@pytest.mark.asyncio
async def test_other_bot_waits_for_the_retry_of_an_owner_that_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot that does not post the transcript replies only once the bot that does has posted it on its retry."""
    monkeypatch.setattr(visible_voice_echo, "_OWNER_RECEIPT_GRACE_SECONDS", 30.0)
    config = _config(tmp_path)
    room = _room_with_members(config, "general", "research")
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    await _owner_fails_to_post(config, tmp_path, room, event)
    other = _build_harness(config, tmp_path, agent_name="research")
    _answers(other, monkeypatch)

    other_delivery = asyncio.create_task(_deliver(other, room, event))
    await asyncio.sleep(0.3)
    assert other.runner.requests == []
    owner = _build_harness(config, tmp_path)
    await _deliver(owner, room, event)
    await asyncio.wait_for(other_delivery, timeout=5)

    assert [request.response_text for request in owner.gateway.sent] == [_TRANSCRIPT]
    assert other.gateway.sent == []
    assert len(owner.runner.requests) == len(other.runner.requests) == 1


@pytest.mark.asyncio
async def test_other_bot_replies_after_the_grace_when_the_owner_never_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait for another bot's transcript is bounded: the reply follows a short grace."""
    monkeypatch.setattr(visible_voice_echo, "_OWNER_RECEIPT_GRACE_SECONDS", 0.2)
    config = _config(tmp_path)
    room = _room_with_members(config, "general", "research")
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    await _owner_fails_to_post(config, tmp_path, room, event)
    other = _build_harness(config, tmp_path, agent_name="research")
    _answers(other, monkeypatch)
    started = time.monotonic()

    await _deliver(other, room, event)

    assert time.monotonic() - started >= 0.2
    assert other.gateway.sent == []
    assert len(other.runner.requests) == 1


@pytest.mark.asyncio
async def test_adaptive_agent_posts_the_transcript_only_once_it_decides_to_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent that still decides whether to answer claims and posts nothing, so the agent that answers posts."""
    config = _config(tmp_path)
    room = _room_with_members(config, "general", "research")
    harness = _build_harness(config, tmp_path)
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    adaptive = ResponseAction(kind="individual", participation=ParticipationConfig())
    plan = DispatchPlan(kind="respond", response_action=adaptive)
    monkeypatch.setattr(harness.policy, "plan_turn", AsyncMock(return_value=plan))

    await _deliver(harness, room, event)

    [request] = harness.runner.requests
    assert harness.gateway.sent == []
    answering = _build_harness(config, tmp_path, agent_name="research")
    _answers(answering, monkeypatch)
    await _deliver(answering, room, event)
    assert [sent.response_text for sent in answering.gateway.sent] == [_TRANSCRIPT]
    # Had the adaptive agent decided to answer too, the one transcript would stand.
    assert request.on_participation_approved is not None
    await request.on_participation_approved()
    assert harness.gateway.sent == []


@pytest.mark.asyncio
async def test_contact_room_voice_is_answered_without_a_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contact room takes only sends its message policy approved, so its voice turn runs and posts no transcript."""
    config = _config(tmp_path)
    harness = _build_harness(config, tmp_path)
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    monkeypatch.setattr("mindroom.ingress_validation.contact_room", lambda *_args: object())

    await _deliver(harness, _room_with_members(config, "general"), event)

    assert harness.gateway.sent == []
    assert [request.prompt for request in harness.runner.requests] == [_TRANSCRIPT]
    assert harness.retried_dispatch_sources == []


@pytest.mark.asyncio
async def test_transcript_replies_to_its_own_voice_message_in_a_coalesced_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn of a voice message and a later text answers the text; the transcript still replies to the audio."""
    config = _config(tmp_path)
    harness = _build_harness(config, tmp_path, debounce_seconds=0.05)
    room = _room_with_members(config, "general")
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)
    text = _text_event("and one more thing", event_id="$text:localhost", thread_id=_THREAD_ROOT)

    await harness.ingress.handle_media_event(room, event)
    await harness.ingress.handle_text_event(room, text)
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    [request] = harness.runner.requests
    assert request.response_envelope.target.reply_to_event_id == text.event_id
    [transcript] = harness.gateway.sent
    assert transcript.target.reply_to_event_id == event.event_id
    assert transcript.target.resolved_thread_id == _THREAD_ROOT


@pytest.mark.asyncio
async def test_echo_off_posts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With `voice.visible_router_echo` off, the bot only replies."""
    config = _config(tmp_path, visible_router_echo=False)
    harness = _build_harness(config, tmp_path)
    event, normalized = _voice_event(config)
    _transcribes(monkeypatch, normalized)

    await _deliver(harness, _room_with_members(config, "general"), event)

    assert harness.gateway.sent == []
    assert len(harness.runner.requests) == 1


@pytest.mark.asyncio
async def test_raw_audio_fallback_posts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A voice message that could not be transcribed has no transcript to post."""
    config = _config(tmp_path)
    harness = _build_harness(config, tmp_path)
    event, normalized = _voice_event(config)
    normalized.source["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    _force_readiness_fallback(monkeypatch)
    monkeypatch.setattr(
        InboundTurnNormalizer,
        "prepare_raw_voice_fallback_event",
        AsyncMock(return_value=_VoiceNormalizationResult(event=normalized)),
    )

    await _deliver(harness, _room_with_members(config, "general"), event)

    assert harness.gateway.sent == []
    assert len(harness.runner.requests) == 1

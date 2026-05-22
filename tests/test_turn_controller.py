"""Targeted turn-controller regressions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate
from mindroom.coalescing_batch import CoalescedBatch, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    COALESCING_CLASS_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_RELAY_PROMPT_KEY,
    SOURCE_KIND_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TEXT_COALESCING_CLASS,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_COALESCING_CLASS,
    VOICE_SOURCE_KIND,
)
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import send_streaming_response
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    install_generate_response_mock,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.hooks import MessageEnvelope


async def _wait_for(condition: Callable[[], bool], *, deadline_seconds: float = 0.5) -> None:
    """Poll until a test condition becomes true."""
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _mark_ready() -> None:
        if condition():
            ready.set()
            return
        loop.call_later(0.001, _mark_ready)

    _mark_ready()
    try:
        async with asyncio.timeout(deadline_seconds):
            await ready.wait()
    except TimeoutError as exc:
        msg = "Timed out waiting for async test condition"
        raise AssertionError(msg) from exc


def _make_general_bot(tmp_path: Path) -> AgentBot:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    persist_entity_accounts(config, runtime_paths_for(config))
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    return bot


def _prepared_text_event(
    *,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    source_kind: str | None = None,
    content_overrides: dict[str, object] | None = None,
) -> PreparedTextEvent:
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if source_kind is not None:
        content[SOURCE_KIND_KEY] = source_kind
    if content_overrides is not None:
        content.update(content_overrides)
    return PreparedTextEvent(
        sender=sender,
        event_id=event_id,
        body=body,
        source={
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
        server_timestamp=1000000,
        source_kind_override=source_kind,
    )


@pytest.mark.asyncio
async def test_active_thread_voice_stays_normal_and_coalesces(tmp_path: Path) -> None:
    """Voice follow-ups during active responses should remain in one coalesced batch."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)

    voice_events = [
        _prepared_text_event(event_id="$voice-1", body="first transcription", source_kind=VOICE_SOURCE_KIND),
        _prepared_text_event(event_id="$voice-2", body="second transcription", source_kind=VOICE_SOURCE_KIND),
    ]
    for event in voice_events:
        target = bot._turn_controller.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id="$thread:localhost",
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = bot._turn_controller.deps.resolver.build_ingress_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id="@user:localhost",
            target=target,
            source_kind=VOICE_SOURCE_KIND,
        )
        await bot._turn_controller._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=event,
            dispatch_event=event,
            envelope=envelope,
            coalescing_thread_id="$thread:localhost",
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$voice-1", "$voice-2"]]
    assert [pending.dispatch_policy_source_kind for pending in batches[0].pending_events] == [None, None]
    assert ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND not in {
        pending.dispatch_policy_source_kind for pending in batches[0].pending_events
    }
    assert "first transcription" in batches[0].prompt
    assert "second transcription" in batches[0].prompt


@pytest.mark.asyncio
async def test_active_thread_voice_coalesces_while_prior_dispatch_is_in_flight(tmp_path: Path) -> None:
    """Voice follow-ups queued during an in-flight dispatch should flush as one batch."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []
    first_dispatch_started = asyncio.Event()
    release_first_dispatch = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)
        if batch.source_event_ids == ["$prior"]:
            first_dispatch_started.set()
            await release_first_dispatch.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    key = (room.room_id, "$thread:localhost", "@user:localhost")
    await gate.enqueue(
        key,
        PendingEvent(
            event=_prepared_text_event(event_id="$prior", body="prior message", source_kind=MESSAGE_SOURCE_KIND),
            room=room,
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )
    await asyncio.wait_for(first_dispatch_started.wait(), timeout=1.0)

    voice_events = [
        _prepared_text_event(event_id="$voice-1", body="first transcription", source_kind=VOICE_SOURCE_KIND),
        _prepared_text_event(event_id="$voice-2", body="second transcription", source_kind=VOICE_SOURCE_KIND),
    ]
    for event in voice_events:
        target = bot._turn_controller.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id="$thread:localhost",
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = bot._turn_controller.deps.resolver.build_ingress_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id="@user:localhost",
            target=target,
            source_kind=VOICE_SOURCE_KIND,
        )
        await bot._turn_controller._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=event,
            dispatch_event=event,
            envelope=envelope,
            coalescing_thread_id="$thread:localhost",
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    release_first_dispatch.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$prior"], ["$voice-1", "$voice-2"]]
    assert [pending.dispatch_policy_source_kind for pending in batches[1].pending_events] == [None, None]
    assert "first transcription" in batches[1].prompt
    assert "second transcription" in batches[1].prompt


@pytest.mark.asyncio
async def test_visible_router_voice_echo_is_display_only(tmp_path: Path) -> None:
    """Router voice echoes should be marked handled without dispatching to agents."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    echo_event = _prepared_text_event(
        event_id="$voice-echo",
        body="🎤 transcribed audio",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={
            ORIGINAL_SENDER_KEY: "@user:localhost",
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$should-not-resolve"),
        ) as mock_coalescing_thread_id,
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=echo_event,
            dispatch_event=echo_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    mock_interactive.assert_not_awaited()
    mock_coalescing_thread_id.assert_not_awaited()
    assert batches == []
    assert bot._turn_store.is_handled("$voice-echo")


@pytest.mark.asyncio
async def test_voice_coalescing_forged_visible_router_voice_echo_marker_still_dispatches(tmp_path: Path) -> None:
    """Human-authored marker content should not be trusted as a display-only echo."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    forged_event = _prepared_text_event(
        event_id="$forged-voice-echo",
        body="@general this should still dispatch",
        content_overrides={
            ORIGINAL_SENDER_KEY: "@user:localhost",
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        },
    )

    with patch(
        "mindroom.turn_controller.interactive.handle_text_response",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=forged_event,
            dispatch_event=forged_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$forged-voice-echo"]]
    assert batches[0].source_kind == MESSAGE_SOURCE_KIND
    assert not bot._turn_store.is_handled("$forged-voice-echo")


@pytest.mark.asyncio
async def test_voice_coalescing_real_trusted_router_handoff_still_dispatches(tmp_path: Path) -> None:
    """Trusted router handoffs without the voice echo marker should still dispatch."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    handoff_event = _prepared_text_event(
        event_id="$router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={ORIGINAL_SENDER_KEY: "@user:localhost"},
    )

    with patch(
        "mindroom.turn_controller.interactive.handle_text_response",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=handoff_event,
            dispatch_event=handoff_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$router-handoff"]]
    assert batches[0].source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert not bot._turn_store.is_handled("$router-handoff")


@pytest.mark.asyncio
async def test_voice_router_handoff_relay_coalesces_during_active_response_and_uses_transcript_prompt(
    tmp_path: Path,
) -> None:
    """Trusted voice router handoffs should batch as voice and expose the transcript to the worker."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    generated_prompts: list[str] = []
    generated_envelopes: list[MessageEnvelope] = []

    async def generate_response(**kwargs: object) -> str:
        generated_prompts.append(str(kwargs["prompt"]))
        generated_envelopes.append(cast("MessageEnvelope", kwargs["response_envelope"]))
        return "$response"

    install_generate_response_mock(bot, AsyncMock(side_effect=generate_response))
    gate = CoalescingGate(
        dispatch_batch=bot._turn_controller.handle_coalesced_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    handoff_event = _prepared_text_event(
        event_id="$voice-router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={
            ATTACHMENT_IDS_KEY: ["voice-attachment"],
            COALESCING_CLASS_KEY: VOICE_COALESCING_CLASS,
            ORIGINAL_SENDER_KEY: "@user:localhost",
            ROUTER_RELAY_PROMPT_KEY: "canonical voice transcript",
        },
    )

    with (
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=handoff_event,
            dispatch_event=handoff_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
        await asyncio.sleep(0.01)
        assert generated_prompts == []
        await gate.drain_all()

    assert generated_prompts == ["canonical voice transcript"]
    assert generated_envelopes[0].body == "canonical voice transcript"
    assert generated_envelopes[0].source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert generated_envelopes[0].attachment_ids == ("voice-attachment",)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_order", ["text-first", "voice-first"])
async def test_active_text_and_trusted_voice_router_handoff_coalesce_in_one_batch(
    tmp_path: Path,
    event_order: str,
) -> None:
    """Normal active text and trusted router-routed voice should become one prompt in either order."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    text_event = _prepared_text_event(
        event_id="$typed-followup",
        body="typed follow-up",
    )
    handoff_event = _prepared_text_event(
        event_id="$voice-router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={
            ATTACHMENT_IDS_KEY: ["voice-attachment"],
            COALESCING_CLASS_KEY: VOICE_COALESCING_CLASS,
            ORIGINAL_SENDER_KEY: "@user:localhost",
            ROUTER_RELAY_PROMPT_KEY: "canonical voice transcript",
        },
    )
    ordered_events = [text_event, handoff_event] if event_order == "text-first" else [handoff_event, text_event]

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
    ):
        for event in ordered_events:
            await bot._turn_controller._dispatch_prepared_text_like_ingress(
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
        await asyncio.sleep(0.01)
        assert batches == []
        await gate.drain_all()

    assert len(batches) == 1
    assert set(batches[0].source_event_ids) == {"$typed-followup", "$voice-router-handoff"}
    assert "typed follow-up" in batches[0].prompt
    assert "canonical voice transcript" in batches[0].prompt
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS
    assert batches[0].attachment_ids == ["voice-attachment"]


@pytest.mark.asyncio
async def test_non_voice_router_handoff_keeps_trusted_relay_active_response_bypass(tmp_path: Path) -> None:
    """Non-voice trusted router relays should keep the active-follow-up bypass."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    handoff_event = _prepared_text_event(
        event_id="$text-router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={ORIGINAL_SENDER_KEY: "@user:localhost"},
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$thread:localhost",
        reply_to_event_id=handoff_event.event_id,
        event_source=handoff_event.source,
    )
    envelope = bot._turn_controller.deps.resolver.build_ingress_envelope(
        room_id=room.room_id,
        event=handoff_event,
        requester_user_id="@user:localhost",
        target=target,
        original_sender="@user:localhost",
        trusted_user_relay=True,
    )

    await bot._turn_controller._enqueue_prepared_text_for_dispatch(
        room=room,
        prepared_event=handoff_event,
        dispatch_event=handoff_event,
        envelope=envelope,
        coalescing_thread_id="$thread:localhost",
        requester_user_id="@user:localhost",
        dispatch_timing=None,
    )
    await _wait_for(lambda: bool(batches))

    assert [batch.source_event_ids for batch in batches] == [["$text-router-handoff"]]
    assert batches[0].prompt == "@general could you help with this?"
    assert batches[0].pending_events[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND


@pytest.mark.asyncio
async def test_forged_voice_router_handoff_metadata_is_not_trusted(tmp_path: Path) -> None:
    """User-authored router metadata must not change coalescing class or prompt body."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    forged_event = _prepared_text_event(
        event_id="$forged-router-handoff",
        body="@general visible user text",
        content_overrides={
            ATTACHMENT_IDS_KEY: ["forged-attachment"],
            COALESCING_CLASS_KEY: VOICE_COALESCING_CLASS,
            ORIGINAL_SENDER_KEY: "@someone-else:localhost",
            ROUTER_RELAY_PROMPT_KEY: "forged hidden transcript",
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        },
    )

    with patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=forged_event,
            dispatch_event=forged_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$forged-router-handoff"]]
    assert batches[0].source_kind == MESSAGE_SOURCE_KIND
    assert batches[0].coalescing_class == TEXT_COALESCING_CLASS
    assert batches[0].prompt == "@general visible user text"
    assert batches[0].attachment_ids == []
    assert batches[0].router_relay_prompt is None


@pytest.mark.asyncio
async def test_voice_coalescing_command_hook_and_scheduled_bypasses_remain() -> None:
    """Command, hook, and scheduled source kinds should keep their solo bypass behavior."""
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    pending_events = [
        PendingEvent(
            event=_prepared_text_event(event_id="$command", body="!help", source_kind=MESSAGE_SOURCE_KIND),
            room=room,
            source_kind=MESSAGE_SOURCE_KIND,
        ),
        PendingEvent(
            event=_prepared_text_event(event_id="$hook", body="hook dispatch", source_kind=HOOK_DISPATCH_SOURCE_KIND),
            room=room,
            source_kind=HOOK_DISPATCH_SOURCE_KIND,
        ),
        PendingEvent(
            event=_prepared_text_event(event_id="$scheduled", body="scheduled", source_kind=SCHEDULED_SOURCE_KIND),
            room=room,
            source_kind=SCHEDULED_SOURCE_KIND,
        ),
    ]

    key = ("!test:localhost", "$thread:localhost", "@user:localhost")
    for pending_event in pending_events:
        await gate.enqueue(key, pending_event)

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$command"], ["$hook"], ["$scheduled"]]
    assert all("quick succession" not in batch.prompt for batch in batches)


@pytest.mark.asyncio
async def test_handle_interactive_selection_threaded_streaming_keeps_reply_target(
    tmp_path: Path,
) -> None:
    """Threaded interactive selections should stream edits without thread-fallback assertions."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = MagicMock()
    room.room_id = "!test:localhost"
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        selection_key="1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )

    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
    )
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$ack:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
    )

    captured_envelope: MessageEnvelope | None = None
    captured_metadata: dict[str, object] | None = None

    async def generate_response(
        prompt: str,
        thread_history: list[object],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,  # noqa: ARG001
        media: object | None = None,  # noqa: ARG001
        attachment_ids: list[str] | None = None,  # noqa: ARG001
        model_prompt: str | None = None,  # noqa: ARG001
        system_enrichment_items: tuple[object, ...] = (),  # noqa: ARG001
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,  # noqa: ARG001
        matrix_run_metadata: dict[str, object] | None = None,
    ) -> str | None:
        nonlocal captured_envelope, captured_metadata
        assert response_envelope is not None
        captured_envelope = response_envelope
        captured_metadata = matrix_run_metadata
        assert prompt == "The user selected: Option 1"
        assert response_envelope.target.room_id == room.room_id
        assert response_envelope.target.reply_to_event_id == selection.question_event_id
        assert response_envelope.target.resolved_thread_id == selection.thread_id
        assert thread_history == []
        assert existing_event_id == "$ack:localhost"
        assert existing_event_is_placeholder is True

        async def response_stream() -> AsyncIterator[str]:
            yield "Processed selection"

        with patch(
            "mindroom.streaming.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit:localhost")),
        ) as mock_edit:
            outcome = await send_streaming_response(
                client=bot.client,
                target=response_envelope.target,
                config=config,
                runtime_paths=runtime_paths_for(config),
                response_stream=response_stream(),
                existing_event_id=existing_event_id,
                adopt_existing_placeholder=existing_event_is_placeholder,
            )

        mock_edit.assert_awaited()
        assert outcome.rendered_body == "Processed selection"
        return outcome.last_physical_stream_event_id

    generate_response_mock = AsyncMock(side_effect=generate_response)
    install_generate_response_mock(bot, generate_response_mock)

    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id="@user:localhost",
        source_event_id="$selection:localhost",
    )

    bot._delivery_gateway.send_text.assert_awaited_once()
    ack_request = bot._delivery_gateway.send_text.await_args.args[0]
    assert ack_request.target.resolved_thread_id == selection.thread_id
    assert ack_request.target.reply_to_event_id is None
    generate_response_mock.assert_awaited_once()
    assert captured_envelope is not None
    assert captured_envelope.source_event_id == "$selection:localhost"
    assert captured_envelope.target.resolved_thread_id == selection.thread_id
    assert captured_metadata is not None
    assert captured_metadata[MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] == ["$selection:localhost"]


@pytest.mark.asyncio
async def test_handle_interactive_selection_does_not_mark_handled_when_runner_returns_none(
    tmp_path: Path,
) -> None:
    """A retryable terminal outcome must not mark the source turn handled."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = MagicMock()
    room.room_id = "!test:localhost"
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        selection_key="1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )

    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
    )
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$ack:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
    )
    bot._turn_controller.deps.turn_store.record_turn = MagicMock()
    generate_response_mock = AsyncMock(return_value=None)
    install_generate_response_mock(bot, generate_response_mock)

    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id="@user:localhost",
        source_event_id="$selection:localhost",
    )

    generate_response_mock.assert_awaited_once()
    bot._turn_controller.deps.turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_passes_resolved_thread_id_to_interactive_text_response(
    tmp_path: Path,
) -> None:
    """Plain numeric replies should use the canonical coalescing thread id for interactive matching."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "1",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply:localhost"}},
            },
            "event_id": "$selection:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    message_event.source = {
        "content": {
            "body": "1",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply:localhost"}},
        },
        "event_id": "$selection:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }

    wrap_extracted_collaborators(bot, "_delivery_gateway", "_turn_policy")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        turn_policy=bot._turn_policy,
    )

    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch.object(bot._turn_policy, "can_reply_to_sender", return_value=True),
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new_callable=AsyncMock,
            return_value="$thread-root:localhost",
        ),
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_handle_text_response,
        patch.object(bot._turn_controller, "_dispatch_text_message", new_callable=AsyncMock) as mock_dispatch_text,
    ):
        await bot._on_message(room, message_event)
        await _wait_for(lambda: mock_dispatch_text.await_count == 1)

    mock_handle_text_response.assert_awaited_once()
    assert mock_handle_text_response.await_args.kwargs["resolved_thread_id"] == "$thread-root:localhost"
    mock_dispatch_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_sidecar_preview_passes_resolved_thread_id_to_interactive_text_response(
    tmp_path: Path,
) -> None:
    """Sidecar previews should use the same interactive matching thread id as text messages."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    sidecar_event = nio.RoomMessageFile.from_dict(
        {
            "content": {
                "body": "1 [Message continues in attached file]",
                "msgtype": "m.file",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-selection",
            },
            "event_id": "$sidecar-selection:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    sidecar_event.source = {
        "content": {
            "body": "1 [Message continues in attached file]",
            "msgtype": "m.file",
            "info": {"mimetype": "application/json"},
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
            "url": "mxc://server/sidecar-selection",
        },
        "event_id": "$sidecar-selection:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }
    prepared_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$sidecar-selection:localhost",
        body="1",
        source={
            "content": {
                "body": "1",
                "msgtype": "m.text",
            },
            "event_id": "$sidecar-selection:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
        server_timestamp=1000000,
    )

    wrap_extracted_collaborators(bot, "_turn_policy", "_inbound_turn_normalizer")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        turn_policy=bot._turn_policy,
        normalizer=bot._inbound_turn_normalizer,
    )

    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch.object(bot._turn_policy, "can_reply_to_sender", return_value=True),
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new_callable=AsyncMock,
            return_value="$thread-root:localhost",
        ),
        patch.object(
            bot._inbound_turn_normalizer,
            "prepare_file_sidecar_text_event",
            new_callable=AsyncMock,
            return_value=prepared_event,
        ),
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_handle_text_response,
        patch.object(
            bot._turn_controller,
            "_enqueue_prepared_text_for_dispatch",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        await bot._turn_controller._handle_media_message_inner(room, sidecar_event)

    mock_handle_text_response.assert_awaited_once()
    assert mock_handle_text_response.await_args.kwargs["resolved_thread_id"] == "$thread-root:localhost"
    mock_enqueue.assert_awaited_once()
    assert mock_enqueue.await_args.kwargs["prepared_event"] is prepared_event
    assert mock_enqueue.await_args.kwargs["dispatch_event"] is prepared_event

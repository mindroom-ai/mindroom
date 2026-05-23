"""Targeted turn-controller regressions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import inbound_turn_normalizer, interactive
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
    HOOK_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
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
from mindroom.timing import DispatchPipelineTiming, attach_dispatch_pipeline_timing
from mindroom.turn_controller import _PrecheckedEvent
from mindroom.turn_ingress_coalescing import (
    BarrierReadyIngressResult,
    IngressProvisionalKey,
    PromptReadyIngressResult,
    TurnIngressCoalescingGate,
)
from tests.conftest import (
    admit_file_sidecar_text_preview_for_test,
    admit_prepared_text_like_ingress_for_test,
    bind_runtime_paths,
    delivered_matrix_side_effect,
    install_generate_response_mock,
    make_visible_message,
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
    server_timestamp: int = 1000000,
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
            "origin_server_ts": server_timestamp,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
        server_timestamp=server_timestamp,
        source_kind_override=source_kind,
    )


def _sidecar_file_event(*, event_id: str, body: str = "preview") -> nio.RoomMessageFile:
    source = {
        "content": {
            "body": body,
            "msgtype": "m.file",
            "info": {"mimetype": "application/json"},
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
            "url": "mxc://server/sidecar",
        },
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }
    event = nio.RoomMessageFile.from_dict(source)
    event.source = source
    return event


def _raw_text_event(
    *,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    server_timestamp: int = 1000000,
) -> nio.RoomMessageText:
    source = {
        "content": {"body": body, "msgtype": "m.text"},
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": server_timestamp,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }
    event = cast("nio.RoomMessageText", nio.RoomMessageText.from_dict(source))
    event.source = source
    return event


def _make_audio_event(
    *,
    event_id: str,
    sender: str = "@user:localhost",
    thread_id: str | None = "$thread:localhost",
    server_timestamp: int = 1000000,
) -> nio.RoomMessageAudio:
    content: dict[str, object] = {"body": "voice.ogg", "msgtype": "m.audio"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.sender = sender
    voice_event.event_id = event_id
    voice_event.body = "voice.ogg"
    voice_event.server_timestamp = server_timestamp
    voice_event.source = {
        "content": content,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": server_timestamp,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }
    return voice_event


def _normalized_voice_result(
    *,
    event: nio.RoomMessageAudio,
    text: str,
    thread_id: str | None,
) -> inbound_turn_normalizer.VoiceNormalizationResult:
    return inbound_turn_normalizer.VoiceNormalizationResult(
        event=PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=text,
            source={
                "content": {
                    "body": text,
                    "msgtype": "m.text",
                    SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id} if thread_id is not None else None,
                },
                "event_id": event.event_id,
                "sender": event.sender,
                "origin_server_ts": event.server_timestamp,
                "type": "m.room.message",
                "room_id": "!test:localhost",
            },
            server_timestamp=event.server_timestamp,
            source_kind_override=VOICE_SOURCE_KIND,
        ),
        effective_thread_id=thread_id,
    )


def _install_turn_batch_capture_gates(
    bot: AgentBot,
    *,
    debounce_seconds: float = 0.02,
    voice_debounce_seconds: float = 0.01,
) -> tuple[CoalescingGate, list[CoalescedBatch]]:
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: voice_debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=ingress_gate)
    return gate, batches


def _replace_turn_dispatch_gates(
    bot: AgentBot,
    gate: CoalescingGate,
    *,
    turn_ingress_debounce_seconds: float,
    turn_ingress_upload_grace_seconds: float = 0.0,
) -> None:
    turn_ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: turn_ingress_debounce_seconds,
        upload_grace_seconds=lambda: turn_ingress_upload_grace_seconds,
        is_shutting_down=lambda: False,
        coalescing_gate=gate,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=turn_ingress_gate)


class _TextAdmissionTestError(Exception):
    """Test-only text admission failure."""


@pytest.mark.asyncio
async def test_build_pending_event_marks_dispatch_timing_gate_enter(tmp_path: Path) -> None:
    """Ready ingress should preserve the pipeline boundary before downstream coalescing."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    event = _prepared_text_event(event_id="$timed", body="timed text")
    timing = DispatchPipelineTiming(source_event_id=event.event_id, room_id=room.room_id)
    attach_dispatch_pipeline_timing(event.source, timing)

    await bot._turn_controller._build_pending_event_for_dispatch(
        event,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        requester_user_id="@user:localhost",
        coalescing_key=(room.room_id, "$thread:localhost", "@user:localhost"),
    )

    assert "gate_enter" in timing.marks


@pytest.mark.asyncio
async def test_raw_text_ingress_cancels_ready_task_when_admission_fails(tmp_path: Path) -> None:
    """Text admission should not leave an unowned ready task when the gate rejects it."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    event = _raw_text_event(event_id="$text-admission-fails", body="hello")
    ready_started = asyncio.Event()
    ready_cancelled = asyncio.Event()

    async def unresolved_ready(**_kwargs: object) -> None:
        ready_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ready_cancelled.set()
            raise

    async def fail_admission(*_args: object, **_kwargs: object) -> None:
        await ready_started.wait()
        raise _TextAdmissionTestError

    try:
        with (
            patch.object(bot._turn_controller, "_ready_raw_text_ingress", new=AsyncMock(side_effect=unresolved_ready)),
            patch.object(
                bot._turn_controller.deps.turn_ingress_gate,
                "admit_ready_task",
                new=AsyncMock(side_effect=fail_admission),
            ),
            pytest.raises(_TextAdmissionTestError),
        ):
            await bot._turn_controller._admit_raw_text_ingress(
                room=room,
                event=event,
                event_info=MagicMock(),
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
        await asyncio.sleep(0)
        production_cancelled = ready_cancelled.is_set()
    finally:
        current_task = asyncio.current_task()
        pending_ready_tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current_task and task.get_name() == "text_ready:$text-admission-fails" and not task.done()
        ]
        for task in pending_ready_tasks:
            task.cancel()
        if pending_ready_tasks:
            await asyncio.gather(*pending_ready_tasks, return_exceptions=True)

    assert production_cancelled


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
    with patch.object(
        bot._turn_controller.deps.resolver,
        "coalescing_thread_id",
        new=AsyncMock(return_value="$thread:localhost"),
    ):
        for event in voice_events:
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )

    await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$voice-1", "$voice-2"]]
    assert [pending.dispatch_policy_source_kind for pending in batches[0].pending_events] == [
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    ]
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
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
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
    with patch.object(
        bot._turn_controller.deps.resolver,
        "coalescing_thread_id",
        new=AsyncMock(return_value="$thread:localhost"),
    ):
        for event in voice_events:
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )

    release_first_dispatch.set()
    await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$prior"], ["$voice-1", "$voice-2"]]
    assert [pending.dispatch_policy_source_kind for pending in batches[1].pending_events] == [
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    ]
    assert "first transcription" in batches[1].prompt
    assert "second transcription" in batches[1].prompt


@pytest.mark.asyncio
async def test_text_received_before_voice_keeps_receive_order_when_text_thread_resolution_is_slow(
    tmp_path: Path,
) -> None:
    """Receive-time text admission keeps a slow first text with the later voice burst."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    gate, batches = _install_turn_batch_capture_gates(bot, debounce_seconds=0.02, voice_debounce_seconds=0.0)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    text_event = _prepared_text_event(event_id="$text", body="typed first")
    voice_event = _make_audio_event(event_id="$voice", thread_id="$thread:localhost")
    resolution_started = asyncio.Event()
    release_resolution = asyncio.Event()
    release_stt = asyncio.Event()

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: PreparedTextEvent | nio.RoomMessageAudio) -> str:
        if event.event_id == "$text":
            resolution_started.set()
            await release_resolution.wait()
        return "$thread:localhost"

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        await release_stt.wait()
        return _normalized_voice_result(
            event=request.event,
            text="canonical voice transcript",
            thread_id="$thread:localhost",
        )

    normalizer = MagicMock(wraps=bot._turn_controller.deps.normalizer)
    normalizer.prepare_voice_event = AsyncMock(side_effect=prepare_voice_event)
    replace_turn_controller_deps(
        bot,
        coalescing_gate=bot._turn_controller.deps.coalescing_gate,
        turn_ingress_gate=bot._turn_controller.deps.turn_ingress_gate,
        normalizer=normalizer,
    )
    text_task: asyncio.Task[None] | None = None
    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(side_effect=coalescing_thread_id),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            text_task = asyncio.create_task(
                admit_prepared_text_like_ingress_for_test(
                    bot._turn_controller,
                    room=room,
                    prepared_event=text_event,
                    dispatch_event=text_event,
                    requester_user_id="@user:localhost",
                    dispatch_timing=None,
                ),
            )
            await asyncio.wait_for(resolution_started.wait(), timeout=1.0)
            await asyncio.sleep(0.08)

            voice_task = asyncio.create_task(bot._turn_controller.handle_media_event(room, voice_event))
            await asyncio.sleep(0.08)

            release_resolution.set()
            release_stt.set()
            await asyncio.wait_for(asyncio.gather(text_task, voice_task), timeout=1.0)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        release_resolution.set()
        release_stt.set()
        for task in (text_task, voice_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert [batch.source_event_ids for batch in batches] == [["$text", "$voice"]]
    assert batches[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS


@pytest.mark.asyncio
async def test_same_room_same_requester_different_threads_are_partitioned_before_voice_retarget(
    tmp_path: Path,
) -> None:
    """Same-room events from one requester must partition by pre-STT thread."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    gate, batches = _install_turn_batch_capture_gates(bot, debounce_seconds=60.0, voice_debounce_seconds=0.0)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=False)

    text_event = _prepared_text_event(
        event_id="$text-thread-a",
        body="typed in thread A",
        content_overrides={"m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-a"}},
    )
    voice_event = _make_audio_event(event_id="$voice-thread-b", thread_id="$thread-b")

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: PreparedTextEvent | nio.RoomMessageAudio) -> str:
        if event.event_id == "$text-thread-a":
            return "$thread-a"
        return "$thread-b"

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        return _normalized_voice_result(
            event=request.event,
            text="voice retargeted to final thread",
            thread_id="$voice-final-thread",
        )

    normalizer = MagicMock(wraps=bot._turn_controller.deps.normalizer)
    normalizer.prepare_voice_event = AsyncMock(side_effect=prepare_voice_event)
    replace_turn_controller_deps(
        bot,
        coalescing_gate=bot._turn_controller.deps.coalescing_gate,
        turn_ingress_gate=bot._turn_controller.deps.turn_ingress_gate,
        normalizer=normalizer,
    )
    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(side_effect=coalescing_thread_id),
        ),
        patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=text_event,
            dispatch_event=text_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
        await bot._turn_controller.handle_media_event(room, voice_event)
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text-thread-a"], ["$voice-thread-b"]]
    assert len(batches) == 2
    batches_by_event_id = {batch.source_event_ids[0]: batch for batch in batches}
    assert batches_by_event_id["$text-thread-a"].coalescing_key == (
        room.room_id,
        "$thread-a",
        "@user:localhost",
    )
    assert batches_by_event_id["$voice-thread-b"].coalescing_key == (
        room.room_id,
        "$voice-final-thread",
        "@user:localhost",
    )


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
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=echo_event,
            dispatch_event=echo_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    mock_interactive.assert_not_awaited()
    mock_coalescing_thread_id.assert_not_awaited()
    assert batches == []
    assert bot._turn_store.is_handled("$voice-echo")


@pytest.mark.asyncio
async def test_visible_router_voice_echo_does_not_split_neighboring_text(tmp_path: Path) -> None:
    """Display-only router echoes should not seal or split a human prompt group."""
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
    _replace_turn_dispatch_gates(bot, gate, turn_ingress_debounce_seconds=60.0)
    text_before = _prepared_text_event(event_id="$text-before", body="first part")
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
    text_after = _prepared_text_event(event_id="$text-after", body="second part")

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
    ):
        for event in (text_before, echo_event, text_after):
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text-before", "$text-after"]]
    assert bot._turn_store.is_handled("$voice-echo")


@pytest.mark.asyncio
async def test_visible_router_voice_echo_does_not_end_upload_grace(tmp_path: Path) -> None:
    """Display-only router echoes should not close a text group that is waiting for media."""
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
    _replace_turn_dispatch_gates(
        bot,
        gate,
        turn_ingress_debounce_seconds=0.01,
        turn_ingress_upload_grace_seconds=0.2,
    )
    text_event = _prepared_text_event(event_id="$text", body="text before media")
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
    provisional_key = IngressProvisionalKey(room_id=room.room_id, requester_user_id="@user:localhost")
    coalescing_key = (room.room_id, "$thread:localhost", "@user:localhost")
    media_pending_event = PendingEvent(
        event=_prepared_text_event(event_id="$media", body="[image]"),
        room=room,
        source_kind=MEDIA_SOURCE_KIND,
    )

    async def media_ready() -> PromptReadyIngressResult:
        return PromptReadyIngressResult(
            pending_event=media_pending_event,
            key=coalescing_key,
            preliminary_key=coalescing_key,
            received_order=0,
            received_wall_time=0.0,
        )

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
    ):
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=text_event,
            dispatch_event=text_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
        await _wait_for(
            lambda: bot._turn_controller.deps.turn_ingress_gate._ingress_grace_groups.get(provisional_key) is not None,
        )
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=echo_event,
            dispatch_event=echo_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
        await bot._turn_controller.deps.turn_ingress_gate.admit_ready_task(
            provisional_key,
            ready_task=asyncio.create_task(media_ready()),
            source_kind=MEDIA_SOURCE_KIND,
            barrier=False,
        )
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text", "$media"]]
    assert bot._turn_store.is_handled("$voice-echo")


@pytest.mark.asyncio
async def test_sidecar_text_preview_during_upload_grace_starts_text_turn(tmp_path: Path) -> None:
    """Long-text sidecar previews should behave like text, not media attachments."""
    bot = _make_general_bot(tmp_path)
    wrap_extracted_collaborators(bot, "_inbound_turn_normalizer")
    replace_turn_controller_deps(bot, normalizer=bot._inbound_turn_normalizer)
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
    _replace_turn_dispatch_gates(
        bot,
        gate,
        turn_ingress_debounce_seconds=0.01,
        turn_ingress_upload_grace_seconds=0.2,
    )
    text_event = _prepared_text_event(event_id="$text", body="text before sidecar")
    sidecar_event = _sidecar_file_event(event_id="$sidecar")
    hydrated_sidecar = _prepared_text_event(event_id="$sidecar", body="hydrated sidecar")
    provisional_key = IngressProvisionalKey(room_id=room.room_id, requester_user_id="@user:localhost")

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch.object(
            bot._inbound_turn_normalizer,
            "prepare_file_sidecar_text_event",
            new=AsyncMock(return_value=hydrated_sidecar),
        ),
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
    ):
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=text_event,
            dispatch_event=text_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
        await _wait_for(
            lambda: bot._turn_controller.deps.turn_ingress_gate._ingress_grace_groups.get(provisional_key) is not None,
        )
        handled = await admit_file_sidecar_text_preview_for_test(
            bot._turn_controller,
            room,
            _PrecheckedEvent(event=sidecar_event, requester_user_id="@user:localhost"),
        )
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert handled is True
    assert [batch.source_event_ids for batch in batches] == [["$text"], ["$sidecar"]]


@pytest.mark.asyncio
async def test_voice_class_forged_visible_router_voice_echo_marker_still_dispatches(tmp_path: Path) -> None:
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
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=forged_event,
            dispatch_event=forged_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$forged-voice-echo"]]
    assert batches[0].source_kind == MESSAGE_SOURCE_KIND
    assert not bot._turn_store.is_handled("$forged-voice-echo")


@pytest.mark.asyncio
async def test_untrusted_source_kind_metadata_does_not_create_receive_time_barrier(tmp_path: Path) -> None:
    """Human-authored source-kind content should not seal prompt groups before ready dispatch."""
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
    _replace_turn_dispatch_gates(bot, gate, turn_ingress_debounce_seconds=60.0)
    forged_hook_event = _prepared_text_event(
        event_id="$forged-hook",
        body="first normal text",
        content_overrides={SOURCE_KIND_KEY: HOOK_SOURCE_KIND},
    )
    follow_up_event = _prepared_text_event(event_id="$follow-up", body="second normal text")

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
    ):
        for event in (forged_hook_event, follow_up_event):
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$forged-hook", "$follow-up"]]
    assert batches[0].source_kind == MESSAGE_SOURCE_KIND


@pytest.mark.asyncio
async def test_voice_class_real_trusted_router_handoff_still_dispatches(tmp_path: Path) -> None:
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
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=handoff_event,
            dispatch_event=handoff_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await bot._turn_controller.deps.turn_ingress_gate.drain_all()
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
    _replace_turn_dispatch_gates(bot, gate, turn_ingress_debounce_seconds=60.0)
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
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=handoff_event,
            dispatch_event=handoff_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
        await asyncio.sleep(0.01)
        assert generated_prompts == []
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
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
    _replace_turn_dispatch_gates(bot, gate, turn_ingress_debounce_seconds=60.0)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    text_timestamp = 2_000_000 if event_order == "voice-first" else 1_000_000
    handoff_timestamp = 1_000_000 if event_order == "voice-first" else 2_000_000
    text_event = _prepared_text_event(
        event_id="$typed-followup",
        body="typed follow-up",
        server_timestamp=text_timestamp,
    )
    handoff_event = _prepared_text_event(
        event_id="$voice-router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        server_timestamp=handoff_timestamp,
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
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
        await asyncio.sleep(0.01)
        assert batches == []
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert len(batches) == 1
    assert set(batches[0].source_event_ids) == {"$typed-followup", "$voice-router-handoff"}
    assert "typed follow-up" in batches[0].prompt
    assert "canonical voice transcript" in batches[0].prompt
    assert batches[0].router_relay_prompt is not None
    assert "typed follow-up" in batches[0].router_relay_prompt
    assert "canonical voice transcript" in batches[0].router_relay_prompt
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS
    assert batches[0].attachment_ids == ["voice-attachment"]


def _assert_split_router_handoff_batches(
    batches: list[CoalescedBatch],
    expected_batches: list[list[str]],
) -> None:
    assert [batch.source_event_ids for batch in batches] == expected_batches
    voice_batch = next(batch for batch in batches if "$router-voice-handoff" in batch.source_event_ids)
    assert voice_batch.dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    assert voice_batch.coalescing_class == VOICE_COALESCING_CLASS
    assert voice_batch.source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert voice_batch.router_relay_prompt is not None
    assert "canonical voice transcript" in voice_batch.router_relay_prompt
    assert all(batch.dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND for batch in batches)


@pytest.mark.asyncio
async def test_media_admission_after_barrier_seals_grace_starts_new_turn() -> None:
    """Media ready probing must not append to a grace group after a barrier sealed it."""
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    provisional_key = IngressProvisionalKey(room_id=room.room_id, requester_user_id="@user:localhost")
    coalescing_key = (room.room_id, "$thread:localhost", "@user:localhost")
    sealed_batches: list[list[str]] = []
    barrier_event_ids: list[str] = []

    class CaptureCoalescingGate:
        async def enqueue_sealed_batch(
            self,
            _key: tuple[str, str | None, str],
            pending_events: list[PendingEvent],
        ) -> None:
            sealed_batches.append([pending_event.event.event_id for pending_event in pending_events])

        async def enqueue(
            self,
            _key: tuple[str, str | None, str],
            pending_event: PendingEvent,
        ) -> None:
            barrier_event_ids.append(pending_event.event.event_id)

    gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
        coalescing_gate=cast("CoalescingGate", CaptureCoalescingGate()),
    )

    async def completed_ready(
        result: PromptReadyIngressResult | BarrierReadyIngressResult,
    ) -> PromptReadyIngressResult | BarrierReadyIngressResult:
        return result

    text_pending = PendingEvent(
        event=_prepared_text_event(event_id="$text", body="text before upload"),
        room=room,
        source_kind=MESSAGE_SOURCE_KIND,
    )
    text_ready_task = asyncio.create_task(
        completed_ready(
            PromptReadyIngressResult(
                pending_event=text_pending,
                key=coalescing_key,
                preliminary_key=coalescing_key,
                received_order=0,
                received_wall_time=0.0,
            ),
        ),
    )
    await text_ready_task
    await gate.admit_ready_task(
        provisional_key,
        ready_task=text_ready_task,
        source_kind=MESSAGE_SOURCE_KIND,
        barrier=False,
    )
    await _wait_for(lambda: gate._ingress_grace_groups.get(provisional_key) is not None)

    media_pending = PendingEvent(
        event=_prepared_text_event(event_id="$media", body="[image]"),
        room=room,
        source_kind=MEDIA_SOURCE_KIND,
    )
    release_media_ready = asyncio.Event()

    async def media_ready() -> PromptReadyIngressResult:
        await release_media_ready.wait()
        return PromptReadyIngressResult(
            pending_event=media_pending,
            key=coalescing_key,
            preliminary_key=coalescing_key,
            received_order=0,
            received_wall_time=0.0,
        )

    barrier_pending = PendingEvent(
        event=_prepared_text_event(event_id="$barrier", body="!help"),
        room=room,
        source_kind=HOOK_SOURCE_KIND,
    )
    barrier_ready_task = asyncio.create_task(
        completed_ready(
            BarrierReadyIngressResult(
                pending_event=barrier_pending,
                key=coalescing_key,
                received_order=0,
                received_wall_time=0.0,
            ),
        ),
    )
    await barrier_ready_task
    media_ready_task = asyncio.create_task(media_ready())
    media_admitted_to_grace = asyncio.Event()
    release_media_admission = asyncio.Event()

    async def block_media_probe_sleep(delay: float) -> None:
        assert delay == 0
        media_admitted_to_grace.set()
        await release_media_admission.wait()

    media_admit_task: asyncio.Task[None] | None = None
    try:
        with patch("mindroom.turn_ingress_coalescing.asyncio.sleep", new=block_media_probe_sleep):
            media_admit_task = asyncio.create_task(
                gate.admit_ready_task(
                    provisional_key,
                    ready_task=media_ready_task,
                    source_kind=None,
                    barrier=False,
                ),
            )
            await asyncio.wait_for(media_admitted_to_grace.wait(), timeout=1.0)
            release_media_ready.set()
            await _wait_for(lambda: media_ready_task.done())
            await gate.admit_ready_task(
                provisional_key,
                ready_task=barrier_ready_task,
                source_kind=HOOK_SOURCE_KIND,
                barrier=True,
            )
            release_media_admission.set()
            await asyncio.wait_for(media_admit_task, timeout=1.0)
    finally:
        release_media_admission.set()
        if media_admit_task is not None and not media_admit_task.done():
            media_admit_task.cancel()
            with suppress(asyncio.CancelledError):
                await media_admit_task

    await gate.drain_all()

    assert sealed_batches == [["$text"], ["$media"]]
    assert barrier_event_ids == ["$barrier"]


async def _run_active_router_handoff_receive_order_sequence(  # noqa: PLR0915
    bot: AgentBot,
    room: nio.MatrixRoom,
    sequence: list[str],
    *,
    slow_text_ids: frozenset[str],
    expected_ids: list[str],
    expected_batches: list[list[str]] | None = None,
) -> list[CoalescedBatch]:
    gate, batches = _install_turn_batch_capture_gates(bot, debounce_seconds=0.02)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)
    text_events = {
        "$text1": _prepared_text_event(event_id="$text1", body="typed follow-up", server_timestamp=1_000_001),
        "$text2": _prepared_text_event(event_id="$text2", body="second typed follow-up", server_timestamp=1_000_002),
    }
    raw_text_events = {
        event_id: _raw_text_event(
            event_id=event.event_id,
            body=event.body,
            server_timestamp=event.server_timestamp or 1_000_000,
        )
        for event_id, event in text_events.items()
    }
    handoff_event = _prepared_text_event(
        event_id="$router-voice-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        server_timestamp=1_000_000,
        content_overrides={
            ATTACHMENT_IDS_KEY: ["voice-attachment"],
            COALESCING_CLASS_KEY: VOICE_COALESCING_CLASS,
            ORIGINAL_SENDER_KEY: "@user:localhost",
            ROUTER_RELAY_PROMPT_KEY: "canonical voice transcript",
        },
    )
    events = {**text_events, "$router-voice-handoff": handoff_event}
    text_resolution_started = {event_id: asyncio.Event() for event_id in slow_text_ids}
    release_text_resolution = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: PreparedTextEvent | nio.RoomMessageText) -> str:
        if event.event_id in slow_text_ids:
            text_resolution_started[event.event_id].set()
            await release_text_resolution.wait()
        return "$thread:localhost"

    async def resolve_text_event(
        request: inbound_turn_normalizer.TextNormalizationRequest,
    ) -> PreparedTextEvent:
        return text_events[request.event.event_id]

    normalizer = MagicMock(wraps=bot._turn_controller.deps.normalizer)
    normalizer.resolve_text_event = AsyncMock(side_effect=resolve_text_event)
    replace_turn_controller_deps(
        bot,
        coalescing_gate=gate,
        turn_ingress_gate=bot._turn_controller.deps.turn_ingress_gate,
        normalizer=normalizer,
    )

    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(side_effect=coalescing_thread_id),
            ),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            for event_id in sequence:
                event = events[event_id]
                if event_id == "$router-voice-handoff":
                    task = asyncio.create_task(
                        admit_prepared_text_like_ingress_for_test(
                            bot._turn_controller,
                            room=room,
                            prepared_event=event,
                            dispatch_event=event,
                            requester_user_id="@user:localhost",
                            dispatch_timing=None,
                        ),
                    )
                else:
                    task = asyncio.create_task(
                        bot._on_message(room, raw_text_events[event_id]),
                    )
                tasks.append(task)
                if event_id in slow_text_ids:
                    await asyncio.wait_for(text_resolution_started[event_id].wait(), timeout=1.0)
                    await asyncio.sleep(0.08)
                    continue
                await asyncio.wait_for(task, timeout=1.0)

            release_text_resolution.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        release_text_resolution.set()
        for task in tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    if expected_batches is not None:
        _assert_split_router_handoff_batches(batches, expected_batches)
        return batches

    assert len(batches) == 1
    assert batches[0].source_event_ids == expected_ids
    assert batches[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS
    assert batches[0].source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert batches[0].router_relay_prompt is not None
    assert "typed follow-up" in batches[0].router_relay_prompt
    assert "canonical voice transcript" in batches[0].router_relay_prompt
    return batches


@pytest.mark.asyncio
async def test_active_router_handoff_text_text_coalesces_when_text_resolution_is_slow(
    tmp_path: Path,
) -> None:
    """Slow text after a router voice relay should still join by receive-time admission."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")

    await _run_active_router_handoff_receive_order_sequence(
        bot,
        room,
        ["$router-voice-handoff", "$text1", "$text2"],
        slow_text_ids=frozenset({"$text1"}),
        expected_ids=["$router-voice-handoff", "$text1", "$text2"],
    )


@pytest.mark.asyncio
async def test_delayed_trusted_voice_router_handoff_keeps_late_text_in_same_turn(
    tmp_path: Path,
) -> None:
    """Receive-time voice-class metadata should hold the turn while relay ready work is pending."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    gate, batches = _install_turn_batch_capture_gates(bot, debounce_seconds=0.02, voice_debounce_seconds=0.02)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    handoff_event = _prepared_text_event(
        event_id="$router-voice-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        server_timestamp=1_000_000,
        content_overrides={
            ATTACHMENT_IDS_KEY: ["voice-attachment"],
            COALESCING_CLASS_KEY: VOICE_COALESCING_CLASS,
            ORIGINAL_SENDER_KEY: "@user:localhost",
            ROUTER_RELAY_PROMPT_KEY: "canonical voice transcript",
        },
    )
    text_event = _prepared_text_event(
        event_id="$typed-followup",
        body="typed follow-up after pending relay",
        server_timestamp=1_000_001,
    )
    handoff_resolution_started = asyncio.Event()
    release_handoff_resolution = asyncio.Event()
    handoff_task: asyncio.Task[None] | None = None

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: PreparedTextEvent) -> str:
        if event.event_id == "$router-voice-handoff":
            handoff_resolution_started.set()
            await release_handoff_resolution.wait()
        return "$thread:localhost"

    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(side_effect=coalescing_thread_id),
            ),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            handoff_task = asyncio.create_task(
                admit_prepared_text_like_ingress_for_test(
                    bot._turn_controller,
                    room=room,
                    prepared_event=handoff_event,
                    dispatch_event=handoff_event,
                    requester_user_id="@user:localhost",
                    dispatch_timing=None,
                ),
            )
            await asyncio.wait_for(handoff_resolution_started.wait(), timeout=1.0)
            await asyncio.wait_for(handoff_task, timeout=1.0)
            await asyncio.sleep(0.08)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=text_event,
                dispatch_event=text_event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
            release_handoff_resolution.set()
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        release_handoff_resolution.set()
        if handoff_task is not None and not handoff_task.done():
            handoff_task.cancel()
            with suppress(asyncio.CancelledError):
                await handoff_task

    assert len(batches) == 1
    assert batches[0].source_event_ids == ["$router-voice-handoff", "$typed-followup"]
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS
    assert batches[0].source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND


@pytest.mark.asyncio
async def test_active_text_text_router_handoff_splits_closed_text_group_when_text_resolution_is_slow(
    tmp_path: Path,
) -> None:
    """Text-only groups closed before a router voice relay should keep receive-order boundaries."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")

    await _run_active_router_handoff_receive_order_sequence(
        bot,
        room,
        ["$text1", "$text2", "$router-voice-handoff"],
        slow_text_ids=frozenset({"$text1"}),
        expected_ids=[],
        expected_batches=[["$text1"], ["$text2", "$router-voice-handoff"]],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_order", ["text-first", "voice-first"])
async def test_mixed_text_and_trusted_voice_router_handoff_dispatches_with_relay_context(
    tmp_path: Path,
    event_order: str,
) -> None:
    """Coalesced router-routed voice plus text should keep router relay semantics in either order."""
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
    _replace_turn_dispatch_gates(bot, gate, turn_ingress_debounce_seconds=60.0)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)
    text_timestamp = 2_000_000 if event_order == "voice-first" else 1_000_000
    handoff_timestamp = 1_000_000 if event_order == "voice-first" else 2_000_000
    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result(
            [
                make_visible_message(
                    sender="@user:localhost",
                    body="typed follow-up",
                    event_id="$typed-followup",
                    timestamp=text_timestamp,
                    thread_id="$thread:localhost",
                ),
            ],
            is_full_history=True,
        ),
    )

    text_event = _prepared_text_event(
        event_id="$typed-followup",
        body="typed follow-up",
        server_timestamp=text_timestamp,
    )
    handoff_event = _prepared_text_event(
        event_id="$voice-router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        server_timestamp=handoff_timestamp,
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
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        for event in ordered_events:
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=event,
                dispatch_event=event,
                requester_user_id="@user:localhost",
                dispatch_timing=None,
            )
        await asyncio.sleep(0.01)
        assert generated_prompts == []
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert len(generated_prompts) == 1
    assert "typed follow-up" in generated_prompts[0]
    assert "canonical voice transcript" in generated_prompts[0]
    assert generated_envelopes[0].source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert generated_envelopes[0].sender_id == "@mindroom_router:localhost"
    assert generated_envelopes[0].requester_id == "@user:localhost"
    assert generated_envelopes[0].attachment_ids == ("voice-attachment",)


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

    with patch.object(
        bot._turn_controller.deps.resolver,
        "coalescing_thread_id",
        new=AsyncMock(return_value="$thread:localhost"),
    ):
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=handoff_event,
            dispatch_event=handoff_event,
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
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=forged_event,
            dispatch_event=forged_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )
    await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$forged-router-handoff"]]
    assert batches[0].source_kind == MESSAGE_SOURCE_KIND
    assert batches[0].coalescing_class == TEXT_COALESCING_CLASS
    assert batches[0].prompt == "@general visible user text"
    assert batches[0].attachment_ids == []
    assert batches[0].router_relay_prompt is None


@pytest.mark.asyncio
async def test_voice_class_command_hook_and_scheduled_bypasses_remain() -> None:
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
            bot._turn_controller.deps.coalescing_gate,
            "enqueue_sealed_batch",
            new_callable=AsyncMock,
        ) as mock_enqueue_sealed_batch,
    ):
        await bot._turn_controller._handle_media_message_inner(room, sidecar_event)
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()

    mock_handle_text_response.assert_awaited_once()
    assert mock_handle_text_response.await_args.kwargs["resolved_thread_id"] == "$thread-root:localhost"
    mock_enqueue_sealed_batch.assert_awaited_once()
    pending_events = mock_enqueue_sealed_batch.await_args.args[1]
    assert pending_events[0].event is prepared_event

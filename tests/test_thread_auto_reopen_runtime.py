"""Runtime coordinator, ingestion pump, bot wiring, and same-turn delivery tests for automatic thread reopen."""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import nio
import pytest
from nio.durable import RecordKind, SyncBatch, SyncRecord
from structlog.testing import capture_logs

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RESOLVED_IN_TURN_KEY, STREAM_STATUS_KEY, STREAM_STATUS_STREAMING
from mindroom.custom_tools.thread_resolution import ThreadResolutionTools
from mindroom.event_journal import AdmissionFacts, IngestionBatchAdmission
from mindroom.final_delivery import Completed, FinalDeliveryOutcome
from mindroom.history.reply_compactions import ReplyCompactions
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from mindroom.message_target import MessageTarget
from mindroom.orchestration.thread_auto_reopen_runtime import ThreadAutoReopenCoordinator
from mindroom.orchestration.thread_snooze_runtime import ThreadSnoozeRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.response_runner import ResponseRunner
from mindroom.streaming import StreamingResponse
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagRecord,
    ThreadTagsError,
    ThreadTagsPermissionError,
    ThreadTagsState,
    _thread_tag_state_key,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

ROOM = "!room:localhost"
ROOT = "$root:localhost"
HUMAN = "@alice:localhost"
SET_AT = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
_REOPEN_FN = "mindroom.orchestration.thread_auto_reopen_runtime.reopen_resolved_thread_record"
_BOT_NUMBERS = itertools.count()


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code")}),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def _bot(*room_ids: str, power: int = 50, name: str | None = None) -> MagicMock:
    idx = next(_BOT_NUMBERS)
    bot = MagicMock()
    bot.agent_name = name or f"bot{idx}"
    bot.running = True
    bot.client = MagicMock(spec=nio.AsyncClient)
    bot.client.user_id = f"@mindroom_bot{idx:04}:localhost"
    bot.client.rooms = {room_id: nio.MatrixRoom(room_id, bot.client.user_id) for room_id in room_ids}
    for room in bot.client.rooms.values():
        room.power_levels.defaults.state_default = 50
        room.power_levels.users[bot.client.user_id] = power
    return bot


def _resolved_event(
    *,
    root: str = ROOT,
    ts: int = 1_000,
    event_id: str = "$state1",
    set_at: datetime = SET_AT,
    content: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = {"set_by": HUMAN, "set_at": set_at.isoformat()} if content is None else content
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": _thread_tag_state_key(root, RESOLVED_THREAD_TAG),
        "sender": HUMAN,
        "event_id": event_id,
        "origin_server_ts": ts,
        "content": payload,
    }


def _reply_event(
    *,
    root: str = ROOT,
    ts: int = 2_000,
    sender: str = HUMAN,
    event_id: str = "$reply1",
    event_type: str = "m.room.message",
    extra_content: dict[str, object] | None = None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": "reopen me",
        "m.relates_to": {"rel_type": "m.thread", "event_id": root},
        **(extra_content or {}),
    }
    return {"type": event_type, "event_id": event_id, "sender": sender, "origin_server_ts": ts, "content": content}


def _coordinators(
    tmp_path: Path,
    bots: dict[str, Any],
    *,
    retry_delays: tuple[float, ...] = (0.0, 0.0),
) -> tuple[ThreadSnoozeRuntimeCoordinator, ThreadAutoReopenCoordinator]:
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    auto_reopen = ThreadAutoReopenCoordinator(paths, lambda: config, lambda: bots, retry_delays=retry_delays)
    snooze = ThreadSnoozeRuntimeCoordinator(paths, lambda: config, lambda: bots, room_state_observer=auto_reopen)
    return snooze, auto_reopen


async def _drain_tasks(coordinator: ThreadAutoReopenCoordinator) -> None:
    for _ in range(10):
        if not coordinator._tasks:
            return
        await asyncio.gather(*list(coordinator._tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_restore_shares_single_room_get_state_drains_buffered_replies_and_respects_live_updates(
    tmp_path: Path,
) -> None:
    """One room_get_state seeds snooze + auto-reopen, live events during restore win, buffered replies drain."""
    bot = _bot(ROOM, name="code")
    gate, entered = asyncio.Event(), asyncio.Event()

    async def slow_state(room_id: str) -> nio.RoomGetStateResponse:
        entered.set()
        await gate.wait()
        return nio.RoomGetStateResponse(
            events=[
                _resolved_event(root="$from-snapshot", ts=1_000, event_id="$s1"),
                _resolved_event(root="$reopened-mid-restore", ts=1_000, event_id="$s2"),
            ],
            room_id=room_id,
        )

    bot.client.room_get_state = AsyncMock(side_effect=slow_state)
    snooze, auto_reopen = _coordinators(tmp_path, {"code": bot})

    auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$from-snapshot", ts=2_000))
    auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$reopened-mid-restore", ts=2_000))

    restore_task = asyncio.create_task(snooze.restore_room(bot, ROOM))
    await entered.wait()
    auto_reopen.observe_state_event(
        ROOM,
        _resolved_event(root="$reopened-mid-restore", ts=1_500, event_id="$s3", content={}),
    )

    reopen_mock = AsyncMock(return_value=True)
    with patch(_REOPEN_FN, new=reopen_mock):
        gate.set()
        await restore_task
        await _drain_tasks(auto_reopen)

    bot.client.room_get_state.assert_awaited_once_with(ROOM)
    reopen_mock.assert_awaited_once()
    assert reopen_mock.await_args is not None
    assert reopen_mock.await_args.args[:3] == (bot.client, ROOM, "$from-snapshot")

    bot.client.rooms = {}
    auto_reopen.room_left(ROOM)
    assert ROOM not in auto_reopen._restored_rooms


@pytest.mark.asyncio
async def test_fast_path_ordering_and_stale_multibot_state_echoes(tmp_path: Path) -> None:
    """Open threads do zero I/O, replies <= resolved_ts do not reopen, stale echoes do not overwrite newer state."""
    bot = _bot(ROOM, name="code")
    _snooze, auto_reopen = _coordinators(tmp_path, {"code": bot})
    auto_reopen.restore_room_events(ROOM, [])

    reopen_mock = AsyncMock(return_value=True)
    with patch(_REOPEN_FN, new=reopen_mock):
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$open", ts=5_000))
        await _drain_tasks(auto_reopen)
        reopen_mock.assert_not_awaited()

        rec = ThreadTagRecord(set_by=HUMAN, set_at=datetime.fromtimestamp(2.0, tz=UTC))
        auto_reopen.resolved(ROOM, ROOT, rec)
        auto_reopen.observe_state_event(ROOM, _resolved_event(root=ROOT, ts=1_950, event_id="$echo", set_at=rec.set_at))
        assert auto_reopen._resolved[ROOM][ROOT].resolved_ts == 1_950
        assert auto_reopen._resolved[ROOM][ROOT].state_event_id == "$echo"

        auto_reopen.observe_state_event(ROOM, _resolved_event(root=ROOT, ts=1_500, event_id="$older"))
        auto_reopen.observe_state_event(ROOM, _resolved_event(root=ROOT, ts=1_900, event_id="$old-tomb", content={}))
        assert auto_reopen._resolved[ROOM][ROOT].state_event_id == "$echo"

        auto_reopen.observe_timeline_event(ROOM, _reply_event(root=ROOT, ts=1_950))
        await _drain_tasks(auto_reopen)
        reopen_mock.assert_not_awaited()

        auto_reopen.observe_timeline_event(ROOM, _reply_event(root=ROOT, ts=1_951))
        await _drain_tasks(auto_reopen)
        reopen_mock.assert_awaited_once()
        assert ROOM not in auto_reopen._resolved


@pytest.mark.asyncio
async def test_single_flight_steward_fallback_and_log_once_permission_warning(tmp_path: Path) -> None:
    """Single-flight dedupes bots, steward falls through on permission, and no-writer warns once per room."""
    steward = _bot(ROOM, power=100, name="router")
    unprivileged = _bot(ROOM, power=0, name="a_low")
    helper = _bot(ROOM, power=50, name="z_helper")
    bots = {"router": steward, "a_low": unprivileged, "z_helper": helper}
    _snooze, auto_reopen = _coordinators(tmp_path, bots)
    auto_reopen.restore_room_events(ROOM, [_resolved_event(root=ROOT, ts=1_000)])

    called_clients: list[object] = []
    denied_err = ThreadTagsPermissionError("denied")

    async def clear_side_effect(client: object, *_args: object, **_kwargs: object) -> bool:
        called_clients.append(client)
        if client is steward.client:
            raise denied_err
        return True

    with patch(_REOPEN_FN, new=AsyncMock(side_effect=clear_side_effect)):
        auto_reopen.observe_timeline_event(ROOM, _reply_event(ts=2_000, event_id="$r1"))
        auto_reopen.observe_timeline_event(ROOM, _reply_event(ts=2_000, event_id="$r1"))
        await _drain_tasks(auto_reopen)
    assert called_clients == [steward.client, helper.client]

    for b in bots.values():
        b.client.rooms[ROOM].power_levels.users[b.client.user_id] = 0
    auto_reopen.observe_state_event(ROOM, _resolved_event(root="$t2", ts=3_000, event_id="$s2"))
    with capture_logs() as logs, patch(_REOPEN_FN, new=AsyncMock()) as mock_reopen:
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$t2", ts=4_000, event_id="$r2"))
        await _drain_tasks(auto_reopen)
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$t2", ts=5_000, event_id="$r3"))
        await _drain_tasks(auto_reopen)
        mock_reopen.assert_not_awaited()
        assert sum(1 for entry in logs if entry.get("event") == "thread_auto_reopen_not_permitted") == 1

        auto_reopen.observe_power_levels_changed(ROOM)
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$t2", ts=6_000, event_id="$r4"))
        await _drain_tasks(auto_reopen)
        assert sum(1 for entry in logs if entry.get("event") == "thread_auto_reopen_not_permitted") == 2


@pytest.mark.asyncio
async def test_bounded_retry_aborts_on_clear_replace_and_stop(tmp_path: Path) -> None:
    """Transient errors retry up to 3 attempts and abort immediately on clear, replace, or stop()."""
    helper = _bot(ROOM, power=50, name="helper")
    _snooze, auto_reopen = _coordinators(tmp_path, {"helper": helper})
    auto_reopen.restore_room_events(ROOM, [_resolved_event(root="$t1", ts=1_000, event_id="$s1")])

    attempts = 0
    transient_err = ThreadTagsError("temporary")

    async def flaky(*_args: object, **_kwargs: object) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise transient_err
        return True

    with patch(_REOPEN_FN, new=AsyncMock(side_effect=flaky)):
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$t1", ts=2_000, event_id="$r1"))
        await _drain_tasks(auto_reopen)
    assert attempts == 3

    auto_reopen.retry_delays = (60.0,)
    auto_reopen.observe_state_event(ROOM, _resolved_event(root="$t2", ts=3_000, event_id="$s2"))
    started = asyncio.Event()

    async def slow_fail(*_args: object, **_kwargs: object) -> bool:
        started.set()
        raise transient_err

    with patch(_REOPEN_FN, new=AsyncMock(side_effect=slow_fail)) as mock_slow:
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$t2", ts=4_000, event_id="$r2"))
        await started.wait()
        await asyncio.sleep(0)
        auto_reopen.observe_state_event(ROOM, _resolved_event(root="$t2", ts=4_500, event_id="$s2-clear", content={}))
        await _drain_tasks(auto_reopen)
        assert mock_slow.await_count == 1

    started.clear()
    auto_reopen.observe_state_event(ROOM, _resolved_event(root="$t3", ts=5_000, event_id="$s3"))
    with patch(_REOPEN_FN, new=AsyncMock(side_effect=slow_fail)):
        auto_reopen.observe_timeline_event(ROOM, _reply_event(root="$t3", ts=6_000, event_id="$r3"))
        await started.wait()
        await asyncio.sleep(0)
        await auto_reopen.stop()
    assert not auto_reopen._tasks
    assert not auto_reopen._in_flight


@pytest.mark.asyncio
async def test_ingestion_batch_and_bot_hooks_forward_qualifying_events_and_skip_history_and_replays(
    tmp_path: Path,
) -> None:
    """consume_one_ingestion_batch + bot hooks feed live/recovered cleartext events and skip HISTORY/replays/ciphertext."""
    bot = _bot(ROOM, name="code")
    _snooze, auto_reopen = _coordinators(tmp_path, {"code": bot})
    roots = ("$t-enc", "$t-sticker", "$t-approval", "$t-stream-notice", "$t-history", "$t-opaque")
    auto_reopen.restore_room_events(
        ROOM,
        [_resolved_event(root=r, ts=1_000, event_id=f"$s-{i}") for i, r in enumerate(roots)],
    )

    records = (
        SyncRecord(
            RecordKind.TIMELINE,
            ROOM,
            {"type": "m.room.encrypted", "event_id": "$e1", "sender": HUMAN, "origin_server_ts": 2_000, "content": {}},
            clear=_reply_event(root="$t-enc", ts=2_000, event_id="$e1"),
            provenance=nio.TimelineEventProvenance.LIVE,
        ),
        SyncRecord(
            RecordKind.TIMELINE,
            ROOM,
            _reply_event(root="$t-sticker", ts=2_000, event_id="$e2", event_type="m.sticker"),
            provenance=nio.TimelineEventProvenance.RECOVERED,
        ),
        SyncRecord(
            RecordKind.TIMELINE,
            ROOM,
            _reply_event(
                root="$t-approval",
                ts=2_000,
                event_id="$e3",
                sender=bot.client.user_id,
                event_type="io.mindroom.tool_approval",
            ),
            provenance=nio.TimelineEventProvenance.LIVE,
        ),
        SyncRecord(
            RecordKind.TIMELINE,
            ROOM,
            _reply_event(
                root="$t-stream-notice",
                ts=2_000,
                event_id="$e4",
                sender=bot.client.user_id,
                extra_content={"msgtype": "m.notice", STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
            ),
            provenance=nio.TimelineEventProvenance.LIVE,
        ),
        SyncRecord(
            RecordKind.TIMELINE,
            ROOM,
            _reply_event(root="$t-history", ts=2_000, event_id="$e5"),
            provenance=nio.TimelineEventProvenance.HISTORY,
        ),
        SyncRecord(
            RecordKind.TIMELINE,
            ROOM,
            {"type": "m.room.encrypted", "event_id": "$e6", "sender": HUMAN, "origin_server_ts": 2_000, "content": {}},
            provenance=nio.TimelineEventProvenance.LIVE,
        ),
    )

    class _Session:
        def __init__(self, batch: SyncBatch) -> None:
            self._batch: SyncBatch | None = batch

        async def next_batch(self) -> SyncBatch | None:
            batch, self._batch = self._batch, None
            return batch

        async def ack(self, _batch: SyncBatch) -> None:
            pass

        async def dispatch(self, _record: SyncRecord, *, event: object | None = None) -> None:
            del event

    class _Admission:
        def __init__(self, *, receipt_new: bool) -> None:
            self._receipt_new = receipt_new

        async def admit_ingestion_batch(self, converted: IngestionBatchAdmission) -> AdmissionFacts:
            return AdmissionFacts(
                self._receipt_new,
                self._receipt_new,
                tuple(AdmissionFacts(self._receipt_new, self._receipt_new) for _ in converted.records),
            )

    bot.orchestrator.thread_auto_reopen = auto_reopen
    reopened_roots: list[str] = []

    async def capture_reopen(_client: object, _room_id: str, thread_root_id: str, **_kwargs: object) -> bool:
        reopened_roots.append(thread_root_id)
        return True

    def on_record(record: SyncRecord) -> None:
        AgentBot._observe_timeline_record(bot, record)

    batch = SyncBatch(uuid4(), 1, records)
    with patch(_REOPEN_FN, new=AsyncMock(side_effect=capture_reopen)):
        await consume_one_ingestion_batch(
            _Session(batch),
            _Admission(receipt_new=False),
            account_id=bot.client.user_id,
            on_timeline_record=on_record,
        )
        await _drain_tasks(auto_reopen)
        assert reopened_roots == []

        await consume_one_ingestion_batch(
            _Session(batch),
            _Admission(receipt_new=True),
            account_id=bot.client.user_id,
            on_timeline_record=on_record,
        )
        await _drain_tasks(auto_reopen)
    assert set(reopened_roots) == {"$t-enc", "$t-sticker", "$t-approval", "$t-stream-notice"}

    # Bot state-event hook forwards clear com.mindroom.thread.tags events and ignores decrypted fakes.
    room = nio.MatrixRoom(ROOM, bot.client.user_id)
    state_event = nio.Event.parse_event(_resolved_event(root="$from-bot-hook", ts=3_000, event_id="$s-hook"))
    assert isinstance(state_event, nio.UnknownEvent)
    await AgentBot._on_room_state_event(bot, room, state_event)
    assert "$from-bot-hook" in auto_reopen._resolved[ROOM]


@pytest.mark.asyncio
async def test_resolve_and_reopen_tools_and_streaming_stamping_prevent_self_reopen(tmp_path: Path) -> None:
    """resolve_thread populates resolved_thread_ids + coordinator; streaming stamps RESOLVED_IN_TURN_KEY."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=config)
    assert orchestrator.thread_auto_reopen in orchestrator._services
    assert orchestrator.thread_snooze.room_state_observer is orchestrator.thread_auto_reopen

    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_code:localhost"
    record = ThreadTagRecord(set_by=HUMAN, set_at=SET_AT)
    resolved_state = ThreadTagsState(room_id=ROOM, thread_root_id=ROOT, tags={RESOLVED_THREAD_TAG: record})
    empty_state = ThreadTagsState(room_id=ROOM, thread_root_id=ROOT, tags={})
    context = ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(ROOM, ROOT, None),
        requester_id=HUMAN,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        plugins=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(client=client),
        orchestrator=orchestrator,
    )

    with (
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock(return_value=resolved_state)),
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock(return_value=empty_state)),
        tool_runtime_context(context),
    ):
        assert json.loads(await ThreadResolutionTools().resolve_thread())["status"] == "ok"
        assert ROOT in context.resolved_thread_ids
        assert ROOT in orchestrator.thread_auto_reopen._resolved[ROOM]

        streaming = StreamingResponse(
            target=context.target,
            config=config,
            runtime_paths=runtime_paths_for(config),
            progress_write_ahead=AsyncMock(),
            progress_delivered=MagicMock(),
            resolved_in_turn=lambda: ROOT in context.resolved_thread_ids,
        )
        assert streaming._delivery_extra_content(is_final=False).get(RESOLVED_IN_TURN_KEY) is True
        assert streaming._delivery_extra_content(is_final=True).get(RESOLVED_IN_TURN_KEY) is True

        assert json.loads(await ThreadResolutionTools().reopen_thread())["status"] == "ok"
        assert ROOT not in context.resolved_thread_ids
        assert ROOM not in orchestrator.thread_auto_reopen._resolved
        assert RESOLVED_IN_TURN_KEY not in streaming._delivery_extra_content(is_final=True)


@pytest.mark.asyncio
async def test_response_runner_stamps_resolved_in_turn_for_collected_and_streamed_turns(tmp_path: Path) -> None:
    """Both collected and streamed ResponseRunner deliveries stamp RESOLVED_IN_TURN_KEY when the turn resolved its thread."""
    config = _config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_code:localhost"
    context = ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(ROOM, ROOT, None),
        requester_id=HUMAN,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        plugins=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(client=client),
    )

    async def stream_resolving_thread(*_args: object) -> AsyncIterator[str]:
        context.resolved_thread_ids.add(ROOT)
        yield "All done."

    runner = MagicMock()
    runner._deliver_collected = ResponseRunner._deliver_collected.__get__(runner, ResponseRunner)
    runner._deliver_streamed = ResponseRunner._deliver_streamed.__get__(runner, ResponseRunner)
    runner._active_response_event_ids = AsyncMock(return_value=set())
    runner._build_compaction_lifecycle = MagicMock(return_value=MagicMock())
    runner._note_final_delivery_timing = MagicMock()
    runner.active_runs = None
    runner.locked.client.return_value = client
    runner.deps.logger = MagicMock()
    runner.deps.delivery_gateway.deliver_final = AsyncMock(return_value=FinalDeliveryOutcome(ending=Completed()))
    runner.deps.delivery_gateway.deliver_stream = AsyncMock(return_value=MagicMock())
    runner.deps.delivery_gateway.finalize_streamed_response = AsyncMock(
        return_value=FinalDeliveryOutcome(ending=Completed()),
    )
    runner.deps.tool_runtime.stream_in_context = MagicMock(side_effect=lambda **_kw: stream_resolving_thread())
    runner.lifecycle_coordinator.visible_progress_callback = MagicMock(return_value=None)

    turn = MagicMock()
    turn.response_kind = "ai"
    turn.run_id = "run-1"
    turn.streaming_cls = StreamingResponse
    turn.stream = MagicMock(side_effect=lambda *_a: stream_resolving_thread())
    turn.request = MagicMock(
        room_id=ROOM,
        existing_event_id=None,
        existing_event_is_placeholder=False,
        attachment_ids=(),
        pipeline_timing=None,
        quiet=True,
    )
    turn.runtime = MagicMock(tool_context=context, show_tool_calls=False, participation=None)

    handle = MagicMock(compactions=ReplyCompactions(), reply_model=None)
    recorder = MagicMock(run_metadata=None, outcome="completed")
    progress = MagicMock()

    with (
        patch("mindroom.response_runner.current_span", return_value=handle),
        patch("mindroom.response_runner.existing_event_id", return_value=None),
        patch("mindroom.response_runner.response_typing_indicator") as typing_cm,
    ):
        typing_cm.return_value.__aenter__ = AsyncMock(return_value=None)
        typing_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        await ResponseRunner._deliver_turn(
            runner,
            turn,
            use_streaming=False,
            recorder=recorder,
            response_identity=MagicMock(),
            progress=progress,
        )
        final_req = runner.deps.delivery_gateway.deliver_final.await_args.args[0]
        assert final_req.extra_content == {RESOLVED_IN_TURN_KEY: True}

        context.resolved_thread_ids.clear()
        await ResponseRunner._deliver_turn(
            runner,
            turn,
            use_streaming=True,
            recorder=recorder,
            response_identity=MagicMock(),
            progress=progress,
        )
        stream_req = runner.deps.delivery_gateway.deliver_stream.await_args.args[0]
        assert stream_req.resolved_in_turn is not None
        assert stream_req.resolved_in_turn() is False
        context.resolved_thread_ids.add(ROOT)
        assert stream_req.resolved_in_turn() is True

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

from mindroom.approval.continuation_runner import ApprovalContinuationRunner
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RESOLVED_IN_TURN_KEY, STREAM_STATUS_KEY, STREAM_STATUS_STREAMING
from mindroom.custom_tools.thread_move import _mark_source_moved
from mindroom.custom_tools.thread_resolution import ThreadResolutionTools
from mindroom.event_journal import AdmissionFacts, IngestionBatchAdmission
from mindroom.final_delivery import Completed, FinalDeliveryOutcome
from mindroom.history.reply_compactions import ReplyCompactions
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from mindroom.message_target import MessageTarget
from mindroom.orchestration.thread_auto_reopen_runtime import ThreadAutoReopenCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.response_runner import ResponseRunner
from mindroom.response_turn import CompletedApprovalRun
from mindroom.streaming import StreamingResponse
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagRecord,
    ThreadTagsState,
    _thread_tag_state_key,
)
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    tool_runtime_context,
    tool_runtime_model_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

ROOM, ROOT, HUMAN = "!room:localhost", "$root:localhost", "@alice:localhost"
SET_AT = datetime.fromtimestamp(1.0, tz=UTC)
_BOT_NUMBERS = itertools.count()


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(Config(agents={"code": AgentConfig(display_name="Code")}), test_runtime_paths(tmp_path))


def _reply(
    root: str = ROOT,
    ts: int = 2_000,
    sender: str = HUMAN,
    event_id: str = "$reply1",
    event_type: str = "m.room.message",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    content = {
        "msgtype": "m.text",
        "body": "reopen",
        "m.relates_to": {"rel_type": "m.thread", "event_id": root},
        **(extra or {}),
    }
    return {"type": event_type, "event_id": event_id, "sender": sender, "origin_server_ts": ts, "content": content}


class _FakeRoomState:
    """In-memory Matrix room state backing real thread_tags reads and writes."""

    def __init__(self) -> None:
        self.tags: dict[str, dict[str, object]] = {}
        self.server_powers: dict[str, int] = {}
        self.before_get: Callable[[str], object | None] | None = None

    def set_tag(self, root: str, tag: str = RESOLVED_THREAD_TAG, *, set_at: datetime = SET_AT) -> str:
        key = _thread_tag_state_key(root, tag)
        self.tags[key] = {"set_by": HUMAN, "set_at": set_at.isoformat()}
        return key

    def attach(self, bot: MagicMock) -> None:
        uid = bot.client.user_id

        async def get_state_event(room_id: str, event_type: str, state_key: str = "") -> object:
            if (
                self.before_get
                and event_type == THREAD_TAGS_EVENT_TYPE
                and (ov := self.before_get(state_key)) is not None
            ):
                return ov
            if event_type == "m.room.power_levels":
                users = {uid: self.server_powers.get(uid, 50), **self.server_powers}
                return nio.RoomGetStateEventResponse({"state_default": 50, "users": users}, event_type, "", room_id)
            if (content := self.tags.get(state_key)) is None:
                return nio.RoomGetStateEventError("missing", status_code="M_NOT_FOUND")
            return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

        async def get_state(room_id: str) -> nio.RoomGetStateResponse:
            events = [
                {"type": THREAD_TAGS_EVENT_TYPE, "state_key": k, "content": dict(v)} for k, v in self.tags.items()
            ]
            return nio.RoomGetStateResponse(events=events, room_id=room_id)

        async def put_state(room_id: str, event_type: str, content: dict[str, object], state_key: str = "") -> object:
            del event_type
            self.tags[state_key] = dict(content)
            return nio.RoomPutStateResponse("$put", room_id)

        bot.client.room_get_state_event = AsyncMock(side_effect=get_state_event)
        bot.client.room_get_state = AsyncMock(side_effect=get_state)
        bot.client.room_put_state = AsyncMock(side_effect=put_state)


def _bot(state: _FakeRoomState, *room_ids: str, power: int = 50, name: str | None = None) -> MagicMock:
    idx = next(_BOT_NUMBERS)
    bot = MagicMock(agent_name=name or f"bot{idx}", running=True)
    bot.client = MagicMock(spec=nio.AsyncClient)
    bot.client.user_id = f"@mindroom_bot{idx:04}:localhost"
    bot.client.rooms = {rid: nio.MatrixRoom(rid, bot.client.user_id) for rid in room_ids}
    for room in bot.client.rooms.values():
        room.power_levels.defaults.state_default = 50
        room.power_levels.users[bot.client.user_id] = power
    state.attach(bot)
    return bot


def _coord(tmp_path: Path, bots: dict[str, Any]) -> ThreadAutoReopenCoordinator:
    config = _config(tmp_path)
    return ThreadAutoReopenCoordinator(runtime_paths_for(config), lambda: config, lambda: bots, retry_delays=(0.0, 0.0))


def _tool_context(tmp_path: Path) -> tuple[Config, _MultiAgentOrchestrator, AsyncMock, ToolRuntimeContext]:
    config = _config(tmp_path)
    orch = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_code:localhost"
    ctx = ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(ROOM, ROOT, None),
        requester_id=HUMAN,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        plugins=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(client=client),
        orchestrator=orch,
    )
    return config, orch, client, ctx


async def _settle() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_open_resolved_older_equal_and_sibling_tags(tmp_path: Path) -> None:
    """Open threads do 1 GET / 0 writes; older/equal replies stay resolved; newer clears resolved preserving siblings."""
    state = _FakeRoomState()
    bot = _bot(state, ROOM, name="code")
    coord = _coord(tmp_path, {"code": bot})

    coord.observe_timeline_event(ROOM, _reply("$open", 2_000, event_id="$r-open"))
    await _settle()
    assert bot.client.room_get_state_event.await_count == 1
    assert bot.client.room_get_state.await_count == 0
    assert bot.client.room_put_state.await_count == 0

    resolved_key, blocked_key = state.set_tag(ROOT), state.set_tag(ROOT, "blocked")
    coord.observe_timeline_event(ROOM, _reply(ROOT, 999, event_id="$r-old"))
    coord.observe_timeline_event(ROOM, _reply(ROOT, 1_000, event_id="$r-eq"))
    await _settle()
    assert state.tags[resolved_key] != {}
    assert bot.client.room_put_state.await_count == 0

    coord.observe_timeline_event(ROOM, _reply(ROOT, 1_001, event_id="$r-new"))
    await _settle()
    assert state.tags[resolved_key] == {}
    assert state.tags[blocked_key]["set_by"] == HUMAN


@pytest.mark.asyncio
async def test_multibot_dedup_steward_fallthrough_and_log_once_warning(tmp_path: Path) -> None:
    """N bots seeing one event do 1 GET; steward permission error falls through; no-writer warns once per room."""
    state = _FakeRoomState()
    steward, low, helper = (
        _bot(state, ROOM, power=100, name="router"),
        _bot(state, ROOM, power=0, name="a"),
        _bot(state, ROOM, power=50, name="z"),
    )
    state.server_powers = {steward.client.user_id: 0, helper.client.user_id: 50}
    bots = {"router": steward, "a": low, "z": helper}
    coord = _coord(tmp_path, bots)

    event = _reply("$open", 2_000, event_id="$same")
    for _ in bots:
        coord.observe_timeline_event(ROOM, event)
    await _settle()
    assert sum(b.client.room_get_state_event.await_count for b in bots.values()) == 1

    resolved_key = state.set_tag(ROOT)
    coord.observe_timeline_event(ROOM, _reply(ROOT, 2_000, event_id="$r-fall"))
    await _settle()
    assert state.tags[resolved_key] == {}
    assert low.client.room_put_state.await_count == 0
    assert helper.client.room_put_state.await_count == 1

    for b in bots.values():
        b.client.rooms[ROOM].power_levels.users[b.client.user_id] = 0
    state.set_tag("$t2")
    with capture_logs() as logs:
        coord.observe_timeline_event(ROOM, _reply("$t2", 3_000, event_id="$r2"))
        await _settle()
        coord.observe_timeline_event(ROOM, _reply("$t2", 4_000, event_id="$r3"))
        await _settle()
    assert sum(1 for entry in logs if entry.get("event") == "thread_auto_reopen_not_permitted") == 1


@pytest.mark.asyncio
async def test_concurrent_re_resolve_and_transient_retry(tmp_path: Path) -> None:
    """expected_record preserves concurrent re-resolve; retries re-read current tag content safely."""
    state = _FakeRoomState()
    bot = _bot(state, ROOM, name="code")
    coord = _coord(tmp_path, {"code": bot})

    resolved_key = state.set_tag(ROOT)
    newer = datetime.fromtimestamp(5.0, tz=UTC)
    orig_get_state = bot.client.room_get_state.side_effect

    async def racy_get_state(room_id: str) -> nio.RoomGetStateResponse:
        state.set_tag(ROOT, set_at=newer)
        return await orig_get_state(room_id)

    bot.client.room_get_state = AsyncMock(side_effect=racy_get_state)
    coord.observe_timeline_event(ROOM, _reply(ROOT, 2_000, event_id="$r-race"))
    await _settle()
    assert state.tags[resolved_key]["set_at"] == newer.isoformat()
    assert bot.client.room_put_state.await_count == 0

    state.attach(bot)
    t2_key, t3_key = state.set_tag("$t2"), state.set_tag("$t3")
    failures = {t2_key: 0, t3_key: 0}

    def flaky_get(state_key: str) -> object | None:
        if state_key == t2_key and failures[t2_key] < 2:
            failures[t2_key] += 1
            return nio.RoomGetStateEventError("temporary", status_code="M_UNKNOWN")
        if state_key == t3_key and failures[t3_key] == 0:
            failures[t3_key] += 1
            state.set_tag("$t3", set_at=newer)
            return nio.RoomGetStateEventError("temporary", status_code="M_UNKNOWN")
        return None

    state.before_get = flaky_get
    coord.observe_timeline_event(ROOM, _reply("$t2", 2_000, event_id="$r-retry"))
    coord.observe_timeline_event(ROOM, _reply("$t3", 2_000, event_id="$r-retry-newer"))
    await _settle()
    assert failures == {t2_key: 2, t3_key: 1}
    assert state.tags[t2_key] == {}
    assert state.tags[t3_key]["set_at"] == newer.isoformat()


@pytest.mark.asyncio
async def test_in_flight_coalescing_and_stop_cancellation(tmp_path: Path) -> None:
    """Newer replies during an in-flight check coalesce; stop() cancels tasks and blocks new work."""
    state = _FakeRoomState()
    bot = _bot(state, ROOM, name="code")
    coord = _coord(tmp_path, {"code": bot})
    t3_key = state.set_tag("$t3")
    entered, gate, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    orig_get_event = bot.client.room_get_state_event.side_effect

    async def gated_get(room_id: str, event_type: str, state_key: str = "") -> object:
        if state_key == t3_key and not entered.is_set():
            entered.set()
            await gate.wait()
        return await orig_get_event(room_id, event_type, state_key)

    bot.client.room_get_state_event = AsyncMock(side_effect=gated_get)
    coord.observe_timeline_event(ROOM, _reply("$t3", 500, event_id="$r-old-flight"))
    await entered.wait()
    coord.observe_timeline_event(ROOM, _reply("$t3", 2_000, event_id="$r-new-flight"))
    gate.set()
    await _settle()
    assert state.tags[t3_key] == {}

    entered.clear()
    gate.clear()

    async def blocking_get(*_args: object, **_kwargs: object) -> object:
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return nio.RoomGetStateEventError("missing", status_code="M_NOT_FOUND")

    bot.client.room_get_state_event = AsyncMock(side_effect=blocking_get)
    coord.observe_timeline_event(ROOM, _reply("$t4", 2_000, event_id="$r-stop"))
    await entered.wait()
    await coord.stop()
    assert cancelled.is_set()
    bot.client.room_get_state_event.reset_mock()
    coord.observe_timeline_event(ROOM, _reply("$t4", 3_000, event_id="$r-after-stop"))
    await _settle()
    assert bot.client.room_get_state_event.await_count == 0


@pytest.mark.asyncio
async def test_ingestion_batch_forwards_live_cleartext_and_skips_history_replay_ciphertext(tmp_path: Path) -> None:
    """consume_one_ingestion_batch + bot hook reopen live/recovered cleartext and skip history/replay/opaque."""
    state = _FakeRoomState()
    bot = _bot(state, ROOM, name="code")
    bot.orchestrator.thread_auto_reopen = _coord(tmp_path, {"code": bot})
    keys = {
        r: state.set_tag(r) for r in ("$t-enc", "$t-sticker", "$t-approval", "$t-stream", "$t-history", "$t-opaque")
    }
    enc = {"type": "m.room.encrypted", "event_id": "$e1", "sender": HUMAN, "origin_server_ts": 2_000, "content": {}}
    prov = nio.TimelineEventProvenance

    def rec(
        src: dict[str, object],
        clear: dict[str, object] | None = None,
        p: nio.TimelineEventProvenance = prov.LIVE,
    ) -> SyncRecord:
        return SyncRecord(RecordKind.TIMELINE, ROOM, src, clear=clear, provenance=p)

    uid = bot.client.user_id
    batch = SyncBatch(
        uuid4(),
        1,
        (
            rec(enc, _reply("$t-enc", event_id="$e1")),
            rec(_reply("$t-sticker", event_id="$e2", event_type="m.sticker"), p=prov.RECOVERED),
            rec(_reply("$t-approval", sender=uid, event_id="$e3", event_type="io.mindroom.tool_approval")),
            rec(
                _reply(
                    "$t-stream",
                    sender=uid,
                    event_id="$e4",
                    extra={"msgtype": "m.notice", STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
                ),
            ),
            rec(_reply("$t-history", event_id="$e5"), p=prov.HISTORY),
            rec({**enc, "event_id": "$e6"}),
        ),
    )

    async def run_batch(*, receipt_new: bool) -> None:
        session = MagicMock(next_batch=AsyncMock(side_effect=[batch, None]), ack=AsyncMock(), dispatch=AsyncMock())

        async def admit(c: IngestionBatchAdmission) -> AdmissionFacts:
            return AdmissionFacts(
                receipt_new,
                receipt_new,
                tuple(AdmissionFacts(receipt_new, receipt_new) for _ in c.records),
            )

        await consume_one_ingestion_batch(
            session,
            MagicMock(admit_ingestion_batch=AsyncMock(side_effect=admit)),
            account_id=uid,
            on_timeline_record=lambda r: AgentBot._observe_timeline_record(bot, r),
        )

    await run_batch(receipt_new=False)
    await _settle()
    assert all(state.tags[k] != {} for k in keys.values())
    await run_batch(receipt_new=True)
    await _settle()
    assert {r for r, k in keys.items() if state.tags[k] == {}} == {"$t-enc", "$t-sticker", "$t-approval", "$t-stream"}


@pytest.mark.asyncio
async def test_thread_resolution_move_and_streaming_stamping(tmp_path: Path) -> None:
    """thread_resolution, thread_move, and StreamingResponse cooperate to stamp RESOLVED_IN_TURN_KEY."""
    config, orch, _client, ctx = _tool_context(tmp_path)
    assert orch.thread_auto_reopen in orch._services
    resolved = ThreadTagsState(
        room_id=ROOM,
        thread_root_id=ROOT,
        tags={RESOLVED_THREAD_TAG: ThreadTagRecord(set_by=HUMAN, set_at=SET_AT)},
    )
    empty = ThreadTagsState(room_id=ROOM, thread_root_id=ROOT, tags={})

    with (
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock(return_value=resolved)),
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock(return_value=empty)),
        tool_runtime_context(ctx),
    ):
        with tool_runtime_model_context("default"):
            assert json.loads(await ThreadResolutionTools().thread_resolution(action="resolve"))["status"] == "ok"
        assert ROOT in ctx.resolved_thread_ids
        streaming = StreamingResponse(
            target=ctx.target,
            config=config,
            runtime_paths=runtime_paths_for(config),
            progress_write_ahead=AsyncMock(),
            progress_delivered=MagicMock(),
            resolved_in_turn=lambda: ROOT in ctx.resolved_thread_ids,
        )
        assert streaming._delivery_extra_content(is_final=False).get(RESOLVED_IN_TURN_KEY) is True
        assert streaming._delivery_extra_content(is_final=True).get(RESOLVED_IN_TURN_KEY) is True
        assert json.loads(await ThreadResolutionTools().thread_resolution(action="reopen"))["status"] == "ok"
        assert ROOT not in ctx.resolved_thread_ids
        assert RESOLVED_IN_TURN_KEY not in streaming._delivery_extra_content(is_final=True)

    warnings: list[str] = []
    with (
        patch("mindroom.custom_tools.thread_move.send_message_result", new=AsyncMock(return_value="$notice")),
        patch("mindroom.custom_tools.thread_move.set_thread_tag", new=AsyncMock(return_value=resolved)),
    ):
        await _mark_source_moved(
            ctx,
            MagicMock(root_id=ROOT, last_event_id="$last"),
            "https://matrix.to/#/$d",
            warnings,
        )
    assert warnings == []
    assert ROOT in ctx.resolved_thread_ids


@pytest.mark.asyncio
async def test_response_runner_and_approval_continuation_stamp_resolved_in_turn(tmp_path: Path) -> None:
    """ResponseRunner (collected + streamed) and ApprovalContinuationRunner stamp RESOLVED_IN_TURN_KEY."""
    _cfg, _orch, client, ctx = _tool_context(tmp_path)
    typing_cm = MagicMock(__aenter__=AsyncMock(return_value=None), __aexit__=AsyncMock(return_value=False))

    async def resolving_stream(*_args: object) -> AsyncIterator[str]:
        ctx.resolved_thread_ids.add(ROOT)
        yield "All done."

    runner = MagicMock(active_runs=None, _active_response_event_ids=AsyncMock(return_value=set()))
    runner._deliver_collected = ResponseRunner._deliver_collected.__get__(runner, ResponseRunner)
    runner._deliver_streamed = ResponseRunner._deliver_streamed.__get__(runner, ResponseRunner)
    runner.locked.client.return_value = client
    done = FinalDeliveryOutcome(ending=Completed())
    runner.deps.delivery_gateway = MagicMock(
        deliver_final=AsyncMock(return_value=done),
        deliver_stream=AsyncMock(),
        finalize_streamed_response=AsyncMock(return_value=done),
    )
    runner.deps.tool_runtime.stream_in_context = MagicMock(side_effect=lambda **_kw: resolving_stream())
    runner.lifecycle_coordinator.visible_progress_callback = MagicMock(return_value=None)
    turn = MagicMock(
        response_kind="ai",
        run_id="r1",
        streaming_cls=StreamingResponse,
        stream=MagicMock(side_effect=lambda *_a: resolving_stream()),
        request=MagicMock(
            room_id=ROOM,
            existing_event_id=None,
            existing_event_is_placeholder=False,
            attachment_ids=(),
            pipeline_timing=None,
            quiet=True,
        ),
        runtime=MagicMock(tool_context=ctx, show_tool_calls=False, participation=None),
    )

    async def deliver(*, use_streaming: bool) -> None:
        await ResponseRunner._deliver_turn(
            runner,
            turn,
            use_streaming=use_streaming,
            recorder=MagicMock(run_metadata=None),
            response_identity=MagicMock(),
            progress=MagicMock(),
        )

    with (
        patch(
            "mindroom.response_runner.current_span",
            return_value=MagicMock(compactions=ReplyCompactions(), reply_model=None),
        ),
        patch("mindroom.response_runner.existing_event_id", return_value=None),
        patch("mindroom.response_runner.response_typing_indicator", return_value=typing_cm),
    ):
        await deliver(use_streaming=False)
        assert runner.deps.delivery_gateway.deliver_final.await_args.args[0].extra_content == {
            RESOLVED_IN_TURN_KEY: True,
        }
        ctx.resolved_thread_ids.clear()
        await deliver(use_streaming=True)
        cb = runner.deps.delivery_gateway.deliver_stream.await_args.args[0].resolved_in_turn
        assert cb is not None
        assert not cb()
        ctx.resolved_thread_ids.add(ROOT)
        assert cb()

    ctx.resolved_thread_ids.clear()
    ar = MagicMock(active_runs=None, _resume_fork_attachments=AsyncMock(return_value=((), ())))
    ar.deps.tool_runtime.build_turn_context.return_value = ctx
    ar.deps.request_preparer.with_system_enrichment = AsyncMock(side_effect=lambda req, **_kw: req)
    ar.locked.client.return_value = client

    async def continue_run(_c: object, *, tool_context: ToolRuntimeContext, **_kw: object) -> CompletedApprovalRun:
        tool_context.resolved_thread_ids.add(ROOT)
        return CompletedApprovalRun(response_text="Done", metadata_content={})

    ar._approval_execution.continue_run = AsyncMock(side_effect=continue_run)
    cont = MagicMock(
        execution_identity=ctx.execution_identity,
        entity_kind="agent",
        entity_name="code",
        requester_id=HUMAN,
        runtime_model_name=None,
        show_tool_calls=False,
        team_member_names=(),
        earlier_calls=(),
        calls=(),
        run_id="r2",
    )
    req = MagicMock(
        quiet=True,
        response_envelope=MagicMock(source_kind="message"),
        turn_id="t1",
        system_enrichment_items=(),
    )
    with (
        patch("mindroom.approval.continuation_runner.correlation_id_for_request", return_value="c1"),
        patch("mindroom.approval.continuation_runner._paused_answer", return_value=MagicMock()),
        patch("mindroom.approval.continuation_runner.response_typing_indicator", return_value=typing_cm),
    ):
        res = await ApprovalContinuationRunner._continue_entity_call(
            ar,
            cont,
            request=req,
            target=ctx.target,
            tool_trace_collector=[],
            progress=None,
            cli_approval_handler=None,
        )
    assert isinstance(res, CompletedApprovalRun)
    assert res.metadata_content == {RESOLVED_IN_TURN_KEY: True}

"""Tests for ReactiveTriggerService runtime, observers, polling, draining, and wiring."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.agent import AgentConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig, GitHubEventSourceConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    AGENT_AUTHORED_KEY,
    SOURCE_KIND_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
)
from mindroom.dispatch_source import REACTIVE_TRIGGER_SOURCE_KIND
from mindroom.event_journal import ConversationCursor, RefreshRequest, RoomPage, VisibleMessage
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.reactive_triggers import (
    REACTIVE_TRIGGER_ID_KEY,
    DispatchResult,
    ExternalEventSourceSpec,
    FiringRecord,
    ReactiveTriggerRecord,
    ReactiveTriggerStore,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriageOutcome,
    TriageSpec,
    TriggerEvent,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.service import (
    _MAX_MESSAGES_PER_POLL,
    _REFETCH_WAIT_SECONDS,
    _ROOM_MEMBER_CACHE_TTL_SECONDS,
    _UNFINISHED_WAIT_SECONDS,
    ReactiveTriggerService,
)
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from tests.conftest import bind_runtime_paths, orchestrator_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_OWNER = "@alice:localhost"
_OTHER = "@bob:localhost"
_UNAUTH = "@stranger:localhost"
_BOT_ACC = "@helper_bot:localhost"
_ROOM_A = "!roomA:localhost"
_ROOM_B = "!roomB:localhost"
_THREAD_A = "$threadA"
_THREAD_B = "$threadB"


def _make_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    reactive_enabled: bool = True,
    agent_users: list[str] | None = None,
    bot_accounts: list[str] | None = None,
    max_fires_per_hour: int = 60,
    event_sources: dict[str, Any] | None = None,
) -> tuple[Config, RuntimePaths]:
    resolved_sources = (
        event_sources
        if event_sources is not None
        else {
            "github": GitHubEventSourceConfig(
                kind="github",
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["mindroom-ai/mindroom"],
                subscribers=[_OWNER],
            ),
        }
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    access={"users": agent_users if agent_users is not None else [_OWNER, _OTHER]},
                ),
            },
            models={
                "default": ModelConfig(provider="openai", id="gpt-test"),
                "cheap": ModelConfig(provider="openai", id="gpt-mini"),
            },
            bot_accounts=bot_accounts or [],
            external_trigger_policy=ExternalTriggerPolicyConfig(
                enabled=enabled,
                reactive_enabled=reactive_enabled,
                max_fires_per_hour=max_fires_per_hour,
                event_sources=resolved_sources,
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    return config, runtime_paths_for(config)


def _visible_message(
    event_id: str,
    *,
    room_id: str = _ROOM_A,
    sender: str = _OTHER,
    body: str = "hello world",
    created_ts: int = 10_000,
    thread_id: str | None = None,
    content_extra: dict[str, Any] | None = None,
) -> VisibleMessage:
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if content_extra:
        content.update(content_extra)
    return VisibleMessage(
        logical_event_id=event_id,
        room_id=room_id,
        thread_id=thread_id,
        sender=sender,
        created_ts=created_ts,
        revision_event_id=event_id,
        revision_ts=created_ts,
        content=content,
    )


def _member_state_source(user_id: str = _OWNER, membership: str = "join") -> dict[str, Any]:
    return {
        "type": "m.room.member",
        "state_key": user_id,
        "content": {"membership": membership},
    }


def _tag_state_source(
    *,
    thread_root_id: str = _THREAD_A,
    tag: str = "resolved",
    present: bool = True,
    event_id: str = "$tag_ev_1",
    sender: str = _OWNER,
    note: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {}
    if present:
        content = {
            "set_by": sender,
            "set_at": "2026-11-22T12:00:00+00:00",
        }
        if note is not None:
            content["note"] = note
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": json.dumps([thread_root_id, tag], separators=(",", ":")),
        "content": content,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 1_763_812_800_000,
    }


def _room_state_snapshot(room_id: str, joined_users: list[str]) -> RoomStateSnapshot:
    return RoomStateSnapshot(
        room_id=room_id,
        events={("m.room.member", uid): {"membership": "join"} for uid in joined_users},
        creators=frozenset(),
    )


@dataclass
class _FakeHydrator:
    repair_room_history: AsyncMock = field(default_factory=AsyncMock)
    resolve_refreshes: AsyncMock = field(default_factory=AsyncMock)


@dataclass
class _FakeConversationReader:
    hydrator: _FakeHydrator = field(default_factory=_FakeHydrator)
    pages: list[RoomPage] = field(default_factory=list)
    latest_cursor: ConversationCursor | None = None
    read_calls: list[tuple[str, ConversationCursor | None, int, int]] = field(default_factory=list)
    latest_cursor_calls: list[str] = field(default_factory=list)

    async def read_room_after(
        self,
        room_id: str,
        after: ConversationCursor | None,
        limit: int,
        until_ts: int,
    ) -> RoomPage:
        self.read_calls.append((room_id, after, limit, until_ts))
        if self.pages:
            return self.pages.pop(0)
        return RoomPage(messages=(), refresh_pending=(), more=False, last_cursor=None, stopped_at=None)

    async def latest_room_cursor(self, room_id: str) -> ConversationCursor | None:
        self.latest_cursor_calls.append(room_id)
        return self.latest_cursor

    async def latest_thread_event_id(self, *, room_id: str, thread_id: str) -> str | None:
        del room_id
        return thread_id


def _make_fake_bot(
    *,
    agent_name: str = "research",
    joined_rooms: list[str] | None = None,
    running: bool = True,
    reader: _FakeConversationReader | None = None,
) -> SimpleNamespace:
    rooms = {
        room_id: nio.MatrixRoom(room_id, f"@mindroom_{agent_name}:localhost")
        for room_id in (joined_rooms if joined_rooms is not None else [_ROOM_A, _ROOM_B])
    }
    client = SimpleNamespace(
        rooms=rooms,
        room_get_state=AsyncMock(
            side_effect=lambda room_id: nio.RoomGetStateResponse([_member_state_source(_OWNER)], room_id),
        ),
    )
    return SimpleNamespace(
        agent_name=agent_name,
        running=running,
        client=client,
        matrix_id=SimpleNamespace(full_id=f"@mindroom_{agent_name}:localhost"),
        conversation_reader=reader or _FakeConversationReader(),
        ingress_validator=None,
    )


def _room_message_rule(
    trigger_id: str = "tr_room_1",
    *,
    room_id: str = _ROOM_A,
    target_room_id: str = _ROOM_B,
    target_thread_id: str | None = _THREAD_B,
    once: bool = False,
    enabled: bool = True,
    expires_at: float | None = None,
    include_bots: bool = False,
    senders: list[str] | None = None,
    pattern: str | None = None,
    triage: TriageSpec | None = None,
) -> ReactiveTriggerRecord:
    now = time.time()
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=_OWNER,
        source=RoomMessageSourceSpec(
            room_id=room_id,
            include_bots=include_bots,
            senders=senders or [],
            pattern=pattern,
        ),
        triage=triage,
        target=TriggerTargetSpec(
            agent="research",
            room_id=target_room_id,
            thread_id=target_thread_id,
            prompt="Handle this message",
        ),
        enabled=enabled,
        once=once,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


def _thread_tag_rule(
    trigger_id: str = "tr_tag_1",
    *,
    room_id: str = _ROOM_A,
    thread_id: str = _THREAD_A,
    tag: str = "resolved",
    transition: str = "added",
    once: bool = True,
    enabled: bool = True,
    expires_at: float | None = None,
) -> ReactiveTriggerRecord:
    now = time.time()
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=_OWNER,
        source=ThreadTagSourceSpec(
            room_id=room_id,
            thread_id=thread_id,
            tag=tag,
            transition=transition,  # type: ignore[arg-type]
        ),
        target=TriggerTargetSpec(
            agent="research",
            room_id=_ROOM_B,
            thread_id=_THREAD_B,
            prompt="Thread A changed, start work on Thread B",
        ),
        enabled=enabled,
        once=once,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


def _external_event_rule(
    trigger_id: str = "tr_ext_1",
    *,
    once: bool = False,
    enabled: bool = True,
    expires_at: float | None = None,
    triage: TriageSpec | None = None,
) -> ReactiveTriggerRecord:
    now = time.time()
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=_OWNER,
        source=ExternalEventSourceSpec(source_id="github", kind="pull_request.merged"),
        triage=triage,
        target=TriggerTargetSpec(
            agent="research",
            room_id=_ROOM_B,
            thread_id=_THREAD_B,
            prompt="PR merged, follow up",
        ),
        enabled=enabled,
        once=once,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


def _external_event(event_id: str = "delivery-1") -> TriggerEvent:
    return TriggerEvent(
        source_type="external_event",
        source_id="github",
        kind="pull_request.merged",
        event_id=event_id,
        timestamp=time.time(),
        data={"repo": "mindroom-ai/mindroom", "number": 4198},
    )


@pytest.mark.asyncio
async def test_zero_rule_fast_path_skips_journal_queries(tmp_path: Path) -> None:
    """Zero enabled room_message rules or disabled policy must do zero journal queries."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    reader = _FakeConversationReader()
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    # No rules at all
    await service.poll_room_messages_once()
    assert reader.read_calls == []
    assert reader.latest_cursor_calls == []
    reader.hydrator.repair_room_history.assert_not_awaited()

    # Only disabled and expired room_message rules
    store.create_trigger(_room_message_rule("tr_disabled", enabled=False))
    store.create_trigger(_room_message_rule("tr_expired", expires_at=time.time() - 10.0))
    await service.poll_room_messages_once()
    assert reader.read_calls == []
    reader.hydrator.repair_room_history.assert_not_awaited()

    # Active rule, but reactive_enabled=False
    store.create_trigger(
        _room_message_rule("tr_active"),
        initial_cursor=RoomCursor(created_ts=1_000, logical_event_id="$init"),
    )
    disabled_config, _ = _make_config(tmp_path, reactive_enabled=False)
    disabled_service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: disabled_config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    await disabled_service.poll_room_messages_once()
    assert reader.read_calls == []
    reader.hydrator.repair_room_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_and_notify_work_wakes_delivery_loop_immediately(tmp_path: Path) -> None:
    """notify_work() wakes the background loop immediately without waiting for the poll tick."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    store.create_trigger(_external_event_rule("tr_wake"))

    delivered_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    orig_mark_delivered = store.mark_delivered

    def _recording_mark_delivered(firing_id: int, matrix_event_id: str, *, now: float | None = None) -> bool:
        updated = orig_mark_delivered(firing_id, matrix_event_id, now=now)
        loop.call_soon_threadsafe(delivered_event.set)
        return updated

    async def _fake_dispatch(**_kwargs: object) -> DispatchResult:
        return DispatchResult(status="delivered", matrix_event_id="$wake_sent_1")

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service._poll_interval_seconds = 60.0

    with (
        patch.object(store, "mark_delivered", side_effect=_recording_mark_delivered),
        patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", side_effect=_fake_dispatch),
    ):
        await service.sync(config)
        # Give loop a tick to settle into wait()
        await asyncio.sleep(0.05)

        store.enqueue_firing("tr_wake", "deliv-wake-1", _external_event("deliv-wake-1"))
        service.notify_work()

        async with asyncio.timeout(2.0):
            await delivered_event.wait()

        # Disabling via sync stops the loop cleanly
        disabled_config, _ = _make_config(tmp_path, reactive_enabled=False)
        await service.sync(disabled_config)
        await service.stop()

    firing = store.get_firing(1)
    assert firing is not None
    assert firing.status == "delivered"
    assert firing.matrix_event_id == "$wake_sent_1"


@pytest.mark.asyncio
async def test_thread_tag_live_observer_transitions_edits_and_rearming(tmp_path: Path) -> None:
    """Live thread tag events fire on matching transitions, ignore edits, and re-arm repeating rules."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _thread_tag_rule("tr_added_repeat", transition="added", once=False),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )
    store.create_trigger(
        _thread_tag_rule("tr_removed_repeat", transition="removed", once=False),
        initial_checkpoint=ThreadCheckpoint(tag_present=True, state_event_id="$init_tag", updated_at=time.time()),
    )

    # Non-tag events and unrelated tags/threads are ignored
    service.observe_state_event(_ROOM_A, {"type": "m.room.topic", "state_key": "", "content": {}})
    service.observe_state_event(_ROOM_A, _tag_state_source(tag="other", event_id="$other_1"))
    assert store.list_due_firings() == []

    # 1. absent -> present fires tr_added_repeat, does NOT fire tr_removed_repeat (already present)
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$tag_add_1"))
    due = store.list_due_firings()
    assert len(due) == 1
    assert due[0].trigger_id == "tr_added_repeat"
    assert due[0].dedupe_key == "$tag_add_1"
    assert due[0].event.kind == "thread_tag.added"
    assert due[0].event.data["state_event_id"] == "$tag_add_1"
    assert store.get_thread_checkpoint("tr_added_repeat").tag_present is True  # type: ignore[union-attr]

    # 2. Editing an already-present tag (present -> present) does NOT refire
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$tag_edit_2", note="updated"))
    due = store.list_due_firings()
    assert len(due) == 1
    cp = store.get_thread_checkpoint("tr_added_repeat")
    assert cp is not None
    assert cp.tag_present is True
    assert cp.state_event_id == "$tag_edit_2"

    # 3. Removal (present -> absent) re-arms tr_added_repeat and fires tr_removed_repeat
    service.observe_state_event(_ROOM_A, _tag_state_source(present=False, event_id="$tag_rem_3"))
    due = store.list_due_firings()
    assert len(due) == 2
    assert due[1].trigger_id == "tr_removed_repeat"
    assert due[1].dedupe_key == "$tag_rem_3"
    assert due[1].event.kind == "thread_tag.removed"
    assert store.get_thread_checkpoint("tr_added_repeat").tag_present is False  # type: ignore[union-attr]
    assert store.get_thread_checkpoint("tr_removed_repeat").tag_present is False  # type: ignore[union-attr]

    # 4. Repeated absent (absent -> absent) does NOT refire
    service.observe_state_event(_ROOM_A, _tag_state_source(present=False, event_id="$tag_rem_4"))
    assert len(store.list_due_firings()) == 2

    # 5. Re-addition fires tr_added_repeat a second time and re-arms tr_removed_repeat
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$tag_add_5"))
    due = store.list_due_firings()
    assert len(due) == 3
    assert due[2].trigger_id == "tr_added_repeat"
    assert due[2].dedupe_key == "$tag_add_5"
    assert store.get_thread_checkpoint("tr_removed_repeat").tag_present is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_thread_tag_missing_checkpoint_initializes_without_firing(tmp_path: Path) -> None:
    """Defensive fallback initializes missing checkpoint without misfiring."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    store.create_trigger(_thread_tag_rule("tr_no_cp", transition="added"))
    assert store.get_thread_checkpoint("tr_no_cp") is None

    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$ev_1"))
    assert store.list_due_firings() == []
    cp = store.get_thread_checkpoint("tr_no_cp")
    assert cp is not None
    assert cp.tag_present is True
    assert cp.state_event_id == "$ev_1"


@pytest.mark.asyncio
async def test_restore_room_skips_state_read_when_no_enabled_thread_tag_rules(tmp_path: Path) -> None:
    """restore_room does zero Matrix state reads for rooms without enabled thread_tag rules."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    await service.restore_room(bot, _ROOM_A)
    bot.client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_room_reconciles_offline_transition_once_and_skips_already_present(tmp_path: Path) -> None:
    """restore_room catches offline transitions once and never fires for already-present tags."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            [
                _member_state_source(_OWNER),
                _tag_state_source(thread_root_id=_THREAD_A, tag="resolved", present=True, event_id="$offline_1"),
                _tag_state_source(
                    thread_root_id="$thread_already",
                    tag="resolved",
                    present=True,
                    event_id="$already_1",
                ),
            ],
            _ROOM_A,
        ),
    )
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    # Rule 1: absent at creation -> offline resolved -> must fire ONCE
    store.create_trigger(
        _thread_tag_rule("tr_offline", thread_id=_THREAD_A, transition="added"),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )
    # Rule 2: already resolved at creation -> must NOT fire
    store.create_trigger(
        _thread_tag_rule("tr_already", thread_id="$thread_already", transition="added"),
        initial_checkpoint=ThreadCheckpoint(tag_present=True, state_event_id="$already_1", updated_at=time.time()),
    )

    await service.restore_room(bot, _ROOM_A)
    due = store.list_due_firings()
    assert len(due) == 1
    assert due[0].trigger_id == "tr_offline"
    assert due[0].dedupe_key == "$offline_1"

    # Re-running restore_room does not double-fire
    await service.restore_room(bot, _ROOM_A)
    assert len(store.list_due_firings()) == 1


@pytest.mark.asyncio
async def test_restore_room_live_event_during_restore_wins_over_stale_snapshot(tmp_path: Path) -> None:
    """A live tag event arriving while room_get_state is in flight is not overwritten by the stale snapshot."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _thread_tag_rule("tr_race", transition="added", once=False),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    in_state_read = asyncio.Event()
    release_state_read = asyncio.Event()

    async def _slow_room_get_state(room_id: str) -> nio.RoomGetStateResponse:
        in_state_read.set()
        await release_state_read.wait()
        # Stale snapshot showing tag present with an older event_id
        return nio.RoomGetStateResponse(
            [_member_state_source(_OWNER), _tag_state_source(present=True, event_id="$stale_snapshot")],
            room_id,
        )

    bot.client.room_get_state = AsyncMock(side_effect=_slow_room_get_state)

    restore_task = asyncio.create_task(service.restore_room(bot, _ROOM_A))
    await in_state_read.wait()

    # Live events arrive while restore is in flight: added then removed
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$live_1"))
    service.observe_state_event(_ROOM_A, _tag_state_source(present=False, event_id="$live_2"))

    release_state_read.set()
    await restore_task

    # Checkpoint reflects the live removal (False), NOT the stale snapshot (True), and only $live_1 fired
    cp = store.get_thread_checkpoint("tr_race")
    assert cp is not None
    assert cp.tag_present is False
    assert cp.state_event_id == "$live_2"
    due = store.list_due_firings()
    assert [f.dedupe_key for f in due] == ["$live_1"]


@pytest.mark.asyncio
async def test_room_message_poll_end_to_end_triage_once_and_cursor_persistence(tmp_path: Path) -> None:
    """Room watch drops triage-no without consuming once=True, fires on triage-yes, and advances cursor."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    reader = _FakeConversationReader(
        pages=[
            RoomPage(
                messages=(
                    _visible_message("$msg_noise", body="just chatting", created_ts=20_000),
                    _visible_message("$msg_urgent", body="prod outage!", created_ts=21_000),
                ),
                refresh_pending=(),
                more=False,
                last_cursor=ConversationCursor(created_ts=21_000, logical_event_id="$msg_urgent"),
                stopped_at=None,
            ),
        ],
    )
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule(
            "tr_watch",
            once=True,
            triage=TriageSpec(instructions="Act only on outages", provider="llm", model="cheap"),
        ),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$start"),
    )

    async def _fake_triage(_rule: ReactiveTriggerRecord, event: TriggerEvent, **_kwargs: object) -> TriageOutcome:
        if "outage" in (event.text or ""):
            return TriageOutcome(act=True, reason="llm (cheap)")
        return TriageOutcome(act=False, reason="llm (cheap)")

    snapshot_a = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER])
    snapshot_b = _room_state_snapshot(_ROOM_B, [_OWNER])

    async def _fake_read_room_state(_client: object, room_id: str) -> RoomStateSnapshot:
        return snapshot_a if room_id == _ROOM_A else snapshot_b

    with (
        patch("mindroom.reactive_triggers.service.read_room_state", side_effect=_fake_read_room_state),
        patch("mindroom.reactive_triggers.dispatch.read_room_state", side_effect=_fake_read_room_state),
        patch("mindroom.reactive_triggers.service.evaluate_triage", side_effect=_fake_triage),
        patch(
            "mindroom.reactive_triggers.dispatch.send_matrix_message",
            new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$delivered_outage", content_sent={})),
        ) as send_mock,
    ):
        await service.poll_room_messages_once()
        cursor = store.get_room_cursor("tr_watch")
        assert cursor == RoomCursor(created_ts=21_000, logical_event_id="$msg_urgent")

        await service.drain_due_firings_once()

    send_mock.assert_awaited_once()
    f1 = store.get_firing(1)
    f2 = store.get_firing(2)
    assert f1 is not None
    assert f1.status == "triaged_no"
    assert f2 is not None
    assert f2.status == "delivered"
    assert f2.matrix_event_id == "$delivered_outage"
    rule_after = store.get_trigger("tr_watch")
    assert rule_after is not None
    assert rule_after.enabled is False
    assert rule_after.fire_count == 1


@pytest.mark.asyncio
async def test_room_message_poll_loop_guard_and_bot_filtering(tmp_path: Path) -> None:
    """Loop-guard markers are always skipped; bot/agent messages respect include_bots."""
    config, runtime_paths = _make_config(
        tmp_path,
        agent_users=[_OWNER, _OTHER, _BOT_ACC],
        bot_accounts=[_BOT_ACC],
    )
    store = ReactiveTriggerStore(runtime_paths)

    msgs = (
        _visible_message(
            "$m_loop",
            sender=_OTHER,
            body="re-trigger",
            created_ts=20_000,
            content_extra={SOURCE_KIND_KEY: REACTIVE_TRIGGER_SOURCE_KIND, REACTIVE_TRIGGER_ID_KEY: "tr_other"},
        ),
        _visible_message(
            "$m_agent_marker",
            sender=_OTHER,
            body="agent authored",
            created_ts=20_100,
            content_extra={AGENT_AUTHORED_KEY: True},
        ),
        _visible_message("$m_bot_acc", sender=_BOT_ACC, body="bot says hi", created_ts=20_200),
        _visible_message("$m_human", sender=_OTHER, body="human says hi", created_ts=20_300),
    )
    page = RoomPage(
        messages=msgs,
        refresh_pending=(),
        more=False,
        last_cursor=ConversationCursor(created_ts=20_300, logical_event_id="$m_human"),
        stopped_at=None,
    )
    reader = _FakeConversationReader(pages=[page, page])
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_no_bots", include_bots=False),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )
    store.create_trigger(
        _room_message_rule("tr_with_bots", include_bots=True),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER, _BOT_ACC])
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)):
        await service.poll_room_messages_once()

    due = store.list_due_firings()
    by_trigger: dict[str, list[str]] = {}
    for firing in due:
        by_trigger.setdefault(firing.trigger_id, []).append(firing.dedupe_key)

    # tr_no_bots sees ONLY the human message
    assert by_trigger["tr_no_bots"] == ["$m_human"]
    # tr_with_bots skips $m_loop (ALWAYS!), sees agent-marker, bot account, and human
    assert by_trigger["tr_with_bots"] == ["$m_agent_marker", "$m_bot_acc", "$m_human"]
    assert store.get_room_cursor("tr_no_bots") == RoomCursor(created_ts=20_300, logical_event_id="$m_human")
    assert store.get_room_cursor("tr_with_bots") == RoomCursor(created_ts=20_300, logical_event_id="$m_human")


@pytest.mark.asyncio
async def test_room_message_poll_stops_before_unfinished_reply_and_resolves_refetch(tmp_path: Path) -> None:
    """Poller stops before unfinished streaming replies and resolves pending refetches without passing stopped_at."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)

    refresh_req = RefreshRequest(
        room_id=_ROOM_A,
        thread_id=None,
        logical_event_id="$needs_refetch",
        revision_event_id="$needs_refetch",
        refresh_token=1,
        membership_epoch=1,
    )
    reader = _FakeConversationReader(
        pages=[
            RoomPage(
                messages=(
                    _visible_message("$m1", body="finished", created_ts=20_000),
                    _visible_message(
                        "$m2_streaming",
                        body="typing...",
                        created_ts=20_100,
                        content_extra={STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
                    ),
                    _visible_message("$m3_after", body="after stream", created_ts=20_200),
                ),
                refresh_pending=(refresh_req,),
                more=False,
                last_cursor=ConversationCursor(created_ts=20_200, logical_event_id="$m3_after"),
                stopped_at=ConversationCursor(created_ts=20_300, logical_event_id="$needs_refetch"),
            ),
        ],
    )
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_stream"),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER])
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)):
        await service.poll_room_messages_once()

    # Only $m1 enqueued; cursor stopped at $m1 (before $m2_streaming and $m3_after)
    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m1"]
    assert store.get_room_cursor("tr_stream") == RoomCursor(created_ts=20_000, logical_event_id="$m1")
    reader.hydrator.resolve_refreshes.assert_awaited_once_with((refresh_req,))


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_room_message_poll_enforces_owner_and_sender_auth_and_transient_holds(tmp_path: Path) -> None:
    """Unauthorized senders/unjoined owners are skipped; transient state/membership errors hold the cursor."""
    config, runtime_paths = _make_config(tmp_path, agent_users=[_OWNER, _OTHER])
    store = ReactiveTriggerStore(runtime_paths)

    msg_unauth = _visible_message("$m_unauth", sender=_UNAUTH, body="nope", created_ts=20_000)
    msg_ok = _visible_message("$m_ok", sender=_OTHER, body="ok", created_ts=20_100)
    page = RoomPage(
        messages=(msg_unauth, msg_ok),
        refresh_pending=(),
        more=False,
        last_cursor=ConversationCursor(created_ts=20_100, logical_event_id="$m_ok"),
        stopped_at=None,
    )
    reader = _FakeConversationReader(pages=[page, page, page])
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_auth"),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    # 1. Transient read_room_state failure -> cursor held at $init, nothing enqueued
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=None)):
        await service.poll_room_messages_once()
    assert store.list_due_firings() == []
    assert store.get_room_cursor("tr_auth") == RoomCursor(created_ts=10_000, logical_event_id="$init")

    # 2. Transient ReplyMembershipPendingError -> cursor held at $init, nothing enqueued
    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER, _UNAUTH])
    with (
        patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)),
        patch(
            "mindroom.reactive_triggers.service.is_sender_allowed_for_agent_reply_in_room",
            side_effect=ReplyMembershipPendingError,
        ),
    ):
        await service.poll_room_messages_once()
    assert store.list_due_firings() == []
    assert store.get_room_cursor("tr_auth") == RoomCursor(created_ts=10_000, logical_event_id="$init")

    # 3. Normal run: $m_unauth skipped, $m_ok enqueued, cursor reaches $m_ok
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)):
        await service.poll_room_messages_once()
    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_ok"]
    assert store.get_room_cursor("tr_auth") == RoomCursor(created_ts=20_100, logical_event_id="$m_ok")

    # 4. Owner left source room (after member cache expires) -> message skipped and cursor advances
    service._room_member_cache.clear()
    reader.pages.append(
        RoomPage(
            messages=(_visible_message("$m_after_leave", sender=_OTHER, body="after leave", created_ts=20_200),),
            refresh_pending=(),
            more=False,
            last_cursor=ConversationCursor(created_ts=20_200, logical_event_id="$m_after_leave"),
            stopped_at=None,
        ),
    )
    snapshot_owner_gone = _room_state_snapshot(_ROOM_A, [_OTHER])
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot_owner_gone)):
        await service.poll_room_messages_once()
    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_ok"]
    assert store.get_room_cursor("tr_auth") == RoomCursor(created_ts=20_200, logical_event_id="$m_after_leave")


@pytest.mark.asyncio
async def test_room_message_poll_initializes_missing_cursor_from_rule_created_at(tmp_path: Path) -> None:
    """Missing cursor initializes from rule.created_at so messages since creation are caught without replaying history."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    created_at = 50.0
    msg_after_create = _visible_message("$m_since_create", body="arrived right after create", created_ts=50_500)
    reader = _FakeConversationReader(
        pages=[
            RoomPage(
                messages=(msg_after_create,),
                refresh_pending=(),
                more=False,
                last_cursor=ConversationCursor(created_ts=50_500, logical_event_id="$m_since_create"),
                stopped_at=None,
            ),
        ],
    )
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    rule = _room_message_rule("tr_no_cursor").model_copy(update={"created_at": created_at, "updated_at": created_at})
    store.create_trigger(rule)
    assert store.get_room_cursor("tr_no_cursor") is None

    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER])
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)):
        await service.poll_room_messages_once()

    assert len(reader.read_calls) == 1
    assert reader.read_calls[0][1] == ConversationCursor(created_ts=50_000, logical_event_id="")
    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_since_create"]
    assert store.get_room_cursor("tr_no_cursor") == RoomCursor(created_ts=50_500, logical_event_id="$m_since_create")


@pytest.mark.asyncio
async def test_room_message_poll_caps_messages_per_poll(tmp_path: Path) -> None:
    """Poller requests and processes at most _MAX_MESSAGES_PER_POLL messages per rule per poll."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    overflow_msgs = tuple(
        _visible_message(f"$m_{idx:03d}", body=f"msg {idx}", created_ts=20_000 + idx)
        for idx in range(_MAX_MESSAGES_PER_POLL + 10)
    )
    reader = _FakeConversationReader(
        pages=[
            RoomPage(
                messages=overflow_msgs,
                refresh_pending=(),
                more=True,
                last_cursor=ConversationCursor(created_ts=20_000 + len(overflow_msgs) - 1, logical_event_id="$m_last"),
                stopped_at=None,
            ),
        ],
    )
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    store.create_trigger(
        _room_message_rule("tr_cap"),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )
    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER])
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)):
        await service.poll_room_messages_once()

    assert reader.read_calls[0][2] == _MAX_MESSAGES_PER_POLL
    assert len(store.list_due_firings(limit=100)) == _MAX_MESSAGES_PER_POLL


@pytest.mark.asyncio
async def test_drain_retry_reuses_claimed_triage_and_delivers_on_retry(tmp_path: Path) -> None:
    """Transient dispatch failure retries with backoff without re-running triage."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _external_event_rule(
            "tr_retry",
            triage=TriageSpec(instructions="Act on merge", provider="llm", model="cheap"),
        ),
    )
    store.enqueue_firing("tr_retry", "deliv-1", _external_event("deliv-1"))

    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="llm (cheap)"))
    dispatch_mock = AsyncMock(
        side_effect=[
            DispatchResult(status="retry", reason="matrix_send_failed"),
            DispatchResult(status="delivered", matrix_event_id="$delivered_retry"),
        ],
    )

    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", new=dispatch_mock),
    ):
        await service.drain_due_firings_once()
        f_mid = store.get_firing(1)
        assert f_mid is not None
        assert f_mid.status == "claimed"
        assert f_mid.attempt_count == 1
        assert f_mid.next_retry_at is not None

        # Force retry due immediately and drain again
        with store._write_transaction() as conn:
            conn.execute("UPDATE trigger_firings SET next_retry_at = ? WHERE id = 1", (time.time() - 1.0,))

        await service.drain_due_firings_once()

    # Triage ran ONCE (not re-run on retry); dispatch ran twice and received the persisted triage_reason
    assert triage_mock.await_count == 1
    assert dispatch_mock.await_count == 2
    assert dispatch_mock.await_args_list[1].kwargs["triage_reason"] == "llm (cheap)"
    f_done = store.get_firing(1)
    assert f_done is not None
    assert f_done.status == "delivered"
    assert f_done.matrix_event_id == "$delivered_retry"


@pytest.mark.asyncio
async def test_drain_skips_disabled_and_expired_rules_without_triage(tmp_path: Path) -> None:
    """Pending firings for disabled or expired rules are marked skipped without calling triage."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _external_event_rule(
            "tr_dis",
            enabled=False,
            triage=TriageSpec(instructions="Act", provider="llm", model="cheap"),
        ),
    )
    store.create_trigger(
        _external_event_rule(
            "tr_exp",
            expires_at=time.time() - 5.0,
            triage=TriageSpec(instructions="Act", provider="llm", model="cheap"),
        ),
    )
    store.enqueue_firing("tr_dis", "d1", _external_event("d1"))
    store.enqueue_firing("tr_exp", "d2", _external_event("d2"))

    triage_mock = AsyncMock()
    with patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock):
        await service.drain_due_firings_once()

    triage_mock.assert_not_awaited()
    assert store.get_firing(1).status == "skipped"  # type: ignore[union-attr]
    assert store.get_firing(2).status == "skipped"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_drain_serializes_concurrent_firings_per_trigger_id(tmp_path: Path) -> None:
    """At most one firing per trigger_id may execute concurrently across overlapping drain calls."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(_external_event_rule("tr_serial"))
    store.create_trigger(_external_event_rule("tr_other"))
    store.enqueue_firing("tr_serial", "s1", _external_event("s1"))
    store.enqueue_firing("tr_serial", "s2", _external_event("s2"))
    store.enqueue_firing("tr_other", "o1", _external_event("o1"))

    s1_entered = asyncio.Event()
    release_s1 = asyncio.Event()
    active_by_trigger: dict[str, int] = {}
    max_concurrent_by_trigger: dict[str, int] = {}
    dispatched_keys: list[str] = []

    async def _fake_dispatch(*, rule: ReactiveTriggerRecord, firing: FiringRecord, **_kwargs: object) -> DispatchResult:
        tid = rule.trigger_id
        active_by_trigger[tid] = active_by_trigger.get(tid, 0) + 1
        max_concurrent_by_trigger[tid] = max(max_concurrent_by_trigger.get(tid, 0), active_by_trigger[tid])
        dispatched_keys.append(firing.dedupe_key)
        try:
            if firing.dedupe_key == "s1":
                s1_entered.set()
                await release_s1.wait()
            return DispatchResult(status="delivered", matrix_event_id=f"$sent_{firing.dedupe_key}")
        finally:
            active_by_trigger[tid] -= 1

    with patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", side_effect=_fake_dispatch):
        drain1 = asyncio.create_task(service.drain_due_firings_once())
        await s1_entered.wait()

        # Overlapping drain while s1 of tr_serial is in-flight:
        # must skip BOTH s1 and s2 (same trigger_id), but CAN deliver o1 (different trigger_id)!
        drain2 = asyncio.create_task(service.drain_due_firings_once())
        await drain2
        assert "o1" in dispatched_keys
        assert "s2" not in dispatched_keys

        release_s1.set()
        await drain1

    assert max_concurrent_by_trigger["tr_serial"] == 1
    assert dispatched_keys.count("s1") == 1
    assert dispatched_keys.count("s2") == 1
    assert dispatched_keys.count("o1") == 1


@pytest.mark.asyncio
async def test_orchestrator_bot_and_external_trigger_runtime_wiring(tmp_path: Path) -> None:
    """Orchestrator, AgentBot, and ExternalTriggerRuntimeCoordinator wire ReactiveTriggerService cleanly."""
    from mindroom.bot import AgentBot  # noqa: PLC0415

    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=orchestrator_runtime_paths(tmp_path),
        config=Config(),
    )
    assert isinstance(orchestrator.reactive_triggers, ReactiveTriggerService)
    assert orchestrator.reactive_triggers in orchestrator._services
    assert orchestrator._external_trigger_runtime.on_reactive_event == orchestrator.reactive_triggers.notify_work

    # ExternalTriggerRuntimeCoordinator passes on_reactive_event to bind_external_trigger_runtime
    cb = MagicMock()
    coordinator = ExternalTriggerRuntimeCoordinator(api_enabled=True, on_reactive_event=cb)
    bot = _make_fake_bot()
    with patch("mindroom.api.main.bind_external_trigger_runtime") as bind_mock:
        coordinator.bind_if_ready(Config(), {"research": bot})  # type: ignore[arg-type]
    bind_mock.assert_called_once()
    assert bind_mock.call_args.kwargs["on_reactive_event"] is cb

    # AgentBot._restore_joined_room forwards restore_room
    fake_bot = MagicMock()
    fake_bot.orchestrator.scheduled_tasks.restore_room = AsyncMock()
    fake_bot.orchestrator.thread_snooze.restore_room = AsyncMock()
    fake_bot.orchestrator.reactive_triggers.restore_room = AsyncMock()
    await AgentBot._restore_joined_room(fake_bot, _ROOM_A)
    fake_bot.orchestrator.reactive_triggers.restore_room.assert_awaited_once_with(fake_bot, _ROOM_A)

    # AgentBot._on_room_state_event forwards tag state events and never raises
    source = _tag_state_source(event_id="$live_wire")
    event = nio.Event.parse_event(source)
    event.decrypted = False
    room = nio.MatrixRoom(_ROOM_A, "@mindroom_router:localhost")
    await AgentBot._on_room_state_event(fake_bot, room, event)
    fake_bot.orchestrator.reactive_triggers.observe_state_event.assert_called_once_with(_ROOM_A, source)

    fake_bot.orchestrator.reactive_triggers.observe_state_event.side_effect = RuntimeError("boom")
    await AgentBot._on_room_state_event(fake_bot, room, event)
    fake_bot.logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_restore_room_reconciles_offline_removal_and_retries_unreadable_state(tmp_path: Path) -> None:
    """restore_room catches offline removals (absent state) and schedules retry when room_get_state fails."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _thread_tag_rule("tr_offline_rem", thread_id=_THREAD_A, transition="removed"),
        initial_checkpoint=ThreadCheckpoint(tag_present=True, state_event_id="$was_present", updated_at=time.time()),
    )

    # First call fails with RoomGetStateError -> schedules retry task, no firing yet
    bot.client.room_get_state = AsyncMock(return_value=nio.RoomGetStateError("boom"))
    await service.restore_room(bot, _ROOM_A)
    assert store.list_due_firings() == []
    assert _ROOM_A in service._restore_retries
    await service.stop()
    assert service._restore_retries == {}

    # Next call succeeds with empty state (tag removed offline) -> fires once
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse([_member_state_source(_OWNER)], _ROOM_A),
    )
    await service.restore_room(bot, _ROOM_A)
    due = store.list_due_firings()
    assert len(due) == 1
    assert due[0].trigger_id == "tr_offline_rem"
    assert due[0].event.kind == "thread_tag.removed"
    cp = store.get_thread_checkpoint("tr_offline_rem")
    assert cp is not None
    assert cp.tag_present is False


@pytest.mark.asyncio
async def test_prune_old_firings_delegates_to_store(tmp_path: Path) -> None:
    """_prune_old_firings prunes old finished firings via the backing SQLite store."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    store.create_trigger(_external_event_rule("tr_prune"))
    store.enqueue_firing("tr_prune", "old-1", _external_event("old-1"))
    store.mark_skipped(1, "old")
    old_ts = time.time() - (31 * 86400)
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET updated_at = ? WHERE id = 1", (old_ts,))

    deleted = await service._prune_old_firings()
    assert deleted == 1
    assert store.get_firing(1) is None


@pytest.mark.asyncio
async def test_unfinished_bot_reply_does_not_block_include_bots_false_rule(tmp_path: Path) -> None:
    """An approval-pending/streaming bot reply does not stall an include_bots=False rule."""
    config, runtime_paths = _make_config(tmp_path, bot_accounts=[_BOT_ACC])
    store = ReactiveTriggerStore(runtime_paths)
    reader = _FakeConversationReader(
        pages=[
            RoomPage(
                messages=(
                    _visible_message(
                        "$m_bot_streaming",
                        sender=_BOT_ACC,
                        body="waiting for approval...",
                        created_ts=20_000,
                        content_extra={
                            AGENT_AUTHORED_KEY: True,
                            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
                        },
                    ),
                    _visible_message("$m_human_after", sender=_OTHER, body="important update", created_ts=20_100),
                ),
                refresh_pending=(),
                more=False,
                last_cursor=ConversationCursor(created_ts=20_100, logical_event_id="$m_human_after"),
                stopped_at=None,
            ),
        ],
    )
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_human_only", include_bots=False),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER, _BOT_ACC])
    with patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)):
        await service.poll_room_messages_once()

    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_human_after"]
    assert store.get_room_cursor("tr_human_only") == RoomCursor(created_ts=20_100, logical_event_id="$m_human_after")
    assert "tr_human_only" not in service._unfinished_waits


@pytest.mark.asyncio
async def test_unfinished_eligible_reply_waits_then_proceeds_after_timeout(tmp_path: Path) -> None:
    """An eligible unfinished reply waits initially, then proceeds once _UNFINISHED_WAIT_SECONDS elapses."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)

    stuck_stream_page = RoomPage(
        messages=(
            _visible_message(
                "$m_spoofed_stream",
                sender=_OTHER,
                body="still streaming forever",
                created_ts=20_000,
                content_extra={STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
            ),
            _visible_message("$m_after_stream", sender=_OTHER, body="next msg", created_ts=20_100),
        ),
        refresh_pending=(),
        more=False,
        last_cursor=ConversationCursor(created_ts=20_100, logical_event_id="$m_after_stream"),
        stopped_at=None,
    )
    reader = _FakeConversationReader(pages=[stuck_stream_page, stuck_stream_page, stuck_stream_page])
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_wait_stream"),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    now_mono = [1_000.0]
    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER])
    with (
        patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)),
        patch("mindroom.reactive_triggers.service.time.monotonic", side_effect=lambda: now_mono[0]),
    ):
        # Poll 1: starts wait, holds before $m_spoofed_stream
        await service.poll_room_messages_once()
        assert store.list_due_firings() == []
        assert store.get_room_cursor("tr_wait_stream") == RoomCursor(created_ts=10_000, logical_event_id="$init")
        assert service._unfinished_waits["tr_wait_stream"] == ("$m_spoofed_stream", 1_000.0)

        # Poll 2: before timeout expires -> still holds
        now_mono[0] = 1_000.0 + _UNFINISHED_WAIT_SECONDS - 10.0
        await service.poll_room_messages_once()
        assert store.list_due_firings() == []
        assert store.get_room_cursor("tr_wait_stream") == RoomCursor(created_ts=10_000, logical_event_id="$init")

        # Poll 3: timeout elapsed -> processes $m_spoofed_stream and $m_after_stream and clears wait
        now_mono[0] = 1_000.0 + _UNFINISHED_WAIT_SECONDS + 1.0
        await service.poll_room_messages_once()

    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_spoofed_stream", "$m_after_stream"]
    assert store.get_room_cursor("tr_wait_stream") == RoomCursor(created_ts=20_100, logical_event_id="$m_after_stream")
    assert "tr_wait_stream" not in service._unfinished_waits


@pytest.mark.asyncio
async def test_stuck_refetch_waits_then_skips_after_timeout(tmp_path: Path) -> None:
    """A permanently stuck refetch waits initially, then is skipped after _REFETCH_WAIT_SECONDS."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)

    stuck_cursor = ConversationCursor(created_ts=20_000, logical_event_id="$stuck_refetch")
    refresh_req = RefreshRequest(
        room_id=_ROOM_A,
        thread_id=None,
        logical_event_id="$stuck_refetch",
        revision_event_id="$stuck_refetch",
        refresh_token=1,
        membership_epoch=1,
    )
    stuck_page = RoomPage(
        messages=(),
        refresh_pending=(refresh_req,),
        more=True,
        last_cursor=None,
        stopped_at=stuck_cursor,
    )
    after_page = RoomPage(
        messages=(_visible_message("$m_after_stuck", body="unblocked", created_ts=21_000),),
        refresh_pending=(),
        more=False,
        last_cursor=ConversationCursor(created_ts=21_000, logical_event_id="$m_after_stuck"),
        stopped_at=None,
    )
    # Poll 1 (stuck), Poll 2 (still stuck), Poll 3 (stuck + limit=1 retry check), Poll 4 (after skip)
    reader = _FakeConversationReader(pages=[stuck_page, stuck_page, stuck_page, stuck_page, after_page])
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_refetch"),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    now_mono = [1_000.0]
    snapshot = _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER])
    with (
        patch("mindroom.reactive_triggers.service.read_room_state", new=AsyncMock(return_value=snapshot)),
        patch("mindroom.reactive_triggers.service.time.monotonic", side_effect=lambda: now_mono[0]),
        patch("mindroom.reactive_triggers.service.logger") as mock_logger,
    ):
        # Poll 1: starts refetch wait, cursor stays at $init
        await service.poll_room_messages_once()
        assert store.get_room_cursor("tr_refetch") == RoomCursor(created_ts=10_000, logical_event_id="$init")
        assert service._refetch_waits["tr_refetch"] == ("$stuck_refetch", 1_000.0)

        # Poll 2: before _REFETCH_WAIT_SECONDS -> still waiting
        now_mono[0] = 1_000.0 + _REFETCH_WAIT_SECONDS - 10.0
        await service.poll_room_messages_once()
        assert store.get_room_cursor("tr_refetch") == RoomCursor(created_ts=10_000, logical_event_id="$init")

        # Poll 3: past _REFETCH_WAIT_SECONDS -> logs warning and skips stuck refetch
        now_mono[0] = 1_000.0 + _REFETCH_WAIT_SECONDS + 1.0
        await service.poll_room_messages_once()
        assert store.get_room_cursor("tr_refetch") == RoomCursor(created_ts=20_000, logical_event_id="$stuck_refetch")
        assert "tr_refetch" not in service._refetch_waits
        mock_logger.warning.assert_any_call(
            "reactive_trigger_refetch_skipped",
            trigger_id="tr_refetch",
            room_id=_ROOM_A,
            event_id="$stuck_refetch",
        )

        # Poll 4: subsequent message after the skipped event is processed
        await service.poll_room_messages_once()

    assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_after_stuck"]
    assert store.get_room_cursor("tr_refetch") == RoomCursor(created_ts=21_000, logical_event_id="$m_after_stuck")


@pytest.mark.asyncio
async def test_policy_disable_and_reenable_reconciles_unrestored_thread_tag_rooms(tmp_path: Path) -> None:
    """stop() clears restore/wait state, and re-enabling via sync() reconciles thread_tag rooms."""
    enabled_config, runtime_paths = _make_config(tmp_path, reactive_enabled=True)
    disabled_config, _ = _make_config(tmp_path, reactive_enabled=False)
    active_config = [enabled_config]

    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse([_member_state_source(_OWNER)], _ROOM_A),
    )

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: active_config[0],
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service._poll_interval_seconds = 60.0

    store.create_trigger(
        _thread_tag_rule("tr_reenable", thread_id=_THREAD_A, transition="added"),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    try:
        # Initial enable schedules background reconciliation of _ROOM_A (tag absent -> no firing)
        await service.sync(enabled_config)
        if service._reconcile_task is not None:
            await service._reconcile_task
        assert _ROOM_A in service._restored_rooms
        assert store.list_due_firings() == []

        # Populate transient wait caches, then disable via sync()
        service._refetch_waits["tr_x"] = ("$e1", 10.0)
        service._unfinished_waits["tr_x"] = ("$e2", 10.0)
        service._room_member_cache[_ROOM_A] = (frozenset({_OWNER}), 10.0)

        active_config[0] = disabled_config
        await service.sync(disabled_config)
        assert service._restored_rooms == set()
        assert service._refetch_waits == {}
        assert service._unfinished_waits == {}
        assert service._room_member_cache == {}

        # While disabled, thread becomes resolved in Matrix room state
        bot.client.room_get_state = AsyncMock(
            return_value=nio.RoomGetStateResponse(
                [
                    _member_state_source(_OWNER),
                    _tag_state_source(
                        thread_root_id=_THREAD_A,
                        tag="resolved",
                        present=True,
                        event_id="$while_disabled",
                    ),
                ],
                _ROOM_A,
            ),
        )

        # Re-enabling via sync() reconciles _ROOM_A and catches the missed transition
        active_config[0] = enabled_config
        with patch(
            "mindroom.reactive_triggers.service.dispatch_reactive_trigger",
            new=AsyncMock(return_value=DispatchResult(status="retry", reason="hold_for_check")),
        ):
            await service.sync(enabled_config)
            if service._reconcile_task is not None:
                await service._reconcile_task
            assert _ROOM_A in service._restored_rooms
            firing = store.get_firing(1)
            assert firing is not None
            assert firing.trigger_id == "tr_reenable"
            assert firing.dedupe_key == "$while_disabled"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_room_member_cache_avoids_repeated_state_reads_and_fails_closed_on_expiry(tmp_path: Path) -> None:
    """Room membership is cached across polls within TTL, refreshed after TTL, and fails closed on error."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)

    pages = [
        RoomPage(
            messages=(_visible_message(f"$m_{idx}", body=f"msg {idx}", created_ts=20_000 + idx * 100),),
            refresh_pending=(),
            more=False,
            last_cursor=ConversationCursor(created_ts=20_000 + idx * 100, logical_event_id=f"$m_{idx}"),
            stopped_at=None,
        )
        for idx in range(4)
    ]
    # Poll 3 fails read_room_state and holds $m_2, so Poll 4 sees $m_2 again
    reader = _FakeConversationReader(pages=[pages[0], pages[1], pages[2], pages[2]])
    bot = _make_fake_bot(reader=reader)
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    store.create_trigger(
        _room_message_rule("tr_cache"),
        initial_cursor=RoomCursor(created_ts=10_000, logical_event_id="$init"),
    )

    now_mono = [1_000.0]
    read_state_mock = AsyncMock(
        side_effect=[
            _room_state_snapshot(_ROOM_A, [_OWNER, _OTHER]),
            None,
            _room_state_snapshot(_ROOM_A, [_OTHER]),
        ],
    )
    with (
        patch("mindroom.reactive_triggers.service.read_room_state", new=read_state_mock),
        patch("mindroom.reactive_triggers.service.time.monotonic", side_effect=lambda: now_mono[0]),
    ):
        # Poll 1 (t=1000): fetches room state
        await service.poll_room_messages_once()
        assert read_state_mock.await_count == 1
        assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_0"]

        # Poll 2 (t=1010, within 30s TTL): hits cache, zero additional /state reads
        now_mono[0] = 1_000.0 + _ROOM_MEMBER_CACHE_TTL_SECONDS / 3
        await service.poll_room_messages_once()
        assert read_state_mock.await_count == 1
        assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_0", "$m_1"]

        # Poll 3 (t=1035, TTL expired): refresh returns None -> fails closed (holds cursor at $m_1)
        now_mono[0] = 1_000.0 + _ROOM_MEMBER_CACHE_TTL_SECONDS + 5.0
        await service.poll_room_messages_once()
        assert read_state_mock.await_count == 2
        assert store.get_room_cursor("tr_cache") == RoomCursor(created_ts=20_100, logical_event_id="$m_1")
        assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_0", "$m_1"]

        # Poll 4 (t=1036): refresh succeeds showing owner left -> skips $m_2 and advances cursor
        now_mono[0] += 1.0
        await service.poll_room_messages_once()
        assert read_state_mock.await_count == 3
        assert store.get_room_cursor("tr_cache") == RoomCursor(created_ts=20_200, logical_event_id="$m_2")
        assert [f.dedupe_key for f in store.list_due_firings()] == ["$m_0", "$m_1"]


@pytest.mark.asyncio
async def test_source_access_rechecked_before_triage_and_delivery_for_room_message(tmp_path: Path) -> None:
    """When Alice leaves the watched room after queueing, drainer skips BEFORE triage and BEFORE delivery."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()

    rule = _room_message_rule(
        "tr_leave_room",
        room_id=_ROOM_A,
        target_room_id=_ROOM_B,
        triage=TriageSpec(instructions="Is this urgent?", provider="llm", model="cheap"),
    )
    store.create_trigger(rule)
    store.enqueue_firing(
        "tr_leave_room",
        "$msg-1",
        TriggerEvent(
            source_type="room_message",
            source_id=_ROOM_A,
            kind="room.message",
            event_id="$msg-1",
            timestamp=time.time(),
            room_id=_ROOM_A,
            actor=_OTHER,
            text="prod alert",
        ),
    )

    # Alice left _ROOM_A (only _OTHER remains joined in _ROOM_A, while Alice is still in _ROOM_B)
    async def _room_get_state(room_id: str) -> nio.RoomGetStateResponse:
        if room_id == _ROOM_A:
            return nio.RoomGetStateResponse([_member_state_source(_OTHER)], _ROOM_A)
        return nio.RoomGetStateResponse([_member_state_source(_OWNER)], room_id)

    bot.client.room_get_state = AsyncMock(side_effect=_room_get_state)

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="urgent"))
    send_mock = AsyncMock()
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock),
    ):
        await service.drain_due_firings_once()

    triage_mock.assert_not_awaited()
    send_mock.assert_not_awaited()
    firing = store.get_firing(1)
    assert firing is not None
    assert firing.status == "skipped"
    assert firing.error_message == "source_room_not_joined"


@pytest.mark.asyncio
async def test_cross_room_thread_tag_skipped_when_owner_leaves_watched_room(tmp_path: Path) -> None:
    """When Alice leaves the watched room on a cross-room thread_tag rule, resolution is NOT delivered."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()

    rule = _thread_tag_rule("tr_cross_tag", room_id=_ROOM_A, thread_id=_THREAD_A)
    store.create_trigger(
        rule,
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    # Simulate tag event enqueued, then Alice leaves _ROOM_A before drain
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$res-1"))
    assert len(store.list_due_firings()) == 1

    async def _room_get_state(room_id: str) -> nio.RoomGetStateResponse:
        if room_id == _ROOM_A:
            return nio.RoomGetStateResponse([_member_state_source(_OTHER)], _ROOM_A)
        return nio.RoomGetStateResponse([_member_state_source(_OWNER)], room_id)

    bot.client.room_get_state = AsyncMock(side_effect=_room_get_state)
    send_mock = AsyncMock()
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock):
        await service.drain_due_firings_once()

    send_mock.assert_not_awaited()
    firing = store.get_firing(1)
    assert firing is not None
    assert firing.status == "skipped"
    assert firing.error_message == "source_room_not_joined"

    # And on restore_room when Alice is not joined to _ROOM_A, offline transition is NOT enqueued
    store.create_trigger(
        _thread_tag_rule("tr_cross_tag_restore", room_id=_ROOM_A, thread_id="$threadX"),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            [
                _member_state_source(_OTHER),
                _tag_state_source(thread_root_id="$threadX", present=True, event_id="$res-2"),
            ],
            _ROOM_A,
        ),
    )
    await service.restore_room(bot, _ROOM_A)
    assert store.list_due_firings() == []


@pytest.mark.asyncio
async def test_external_event_subscriber_revoked_during_retry_backoff_skips_retry(tmp_path: Path) -> None:
    """Removing Alice from source.subscribers while a claimed external_event is in retry backoff skips delivery."""
    config_allowed, runtime_paths = _make_config(tmp_path)
    config_revoked, _ = _make_config(
        tmp_path,
        event_sources={
            "github": GitHubEventSourceConfig(
                kind="github",
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["mindroom-ai/mindroom"],
                subscribers=[_OTHER],  # Alice removed!
            ),
        },
    )
    active_config = [config_allowed]

    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse([_member_state_source(_OWNER)], _ROOM_B),
    )
    store.create_trigger(_external_event_rule("tr_ext_revoke"))
    store.enqueue_firing("tr_ext_revoke", "deliv-rev-1", _external_event("deliv-rev-1"))

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: active_config[0],
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    # First attempt claims the firing, then transient Matrix send failure puts it into retry backoff
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=AsyncMock(return_value=None)):
        await service.drain_due_firings_once()

    mid = store.get_firing(1)
    assert mid is not None
    assert mid.status == "claimed"
    assert mid.attempt_count == 1

    # Revoke Alice from subscribers and make retry due
    active_config[0] = config_revoked
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET next_retry_at = ? WHERE id = 1", (time.time() - 1.0,))

    send_mock = AsyncMock()
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock):
        await service.drain_due_firings_once()

    send_mock.assert_not_awaited()
    after = store.get_firing(1)
    assert after is not None
    assert after.status == "skipped"
    assert after.error_message == "source_subscriber_revoked"


@pytest.mark.asyncio
async def test_disabled_thread_tag_rule_ignored_during_live_observation(tmp_path: Path) -> None:
    """Disabled thread_tag rules are ignored by live observation; re-enable refreshes checkpoint separately."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    store.create_trigger(
        _thread_tag_rule("tr_once_rearm", thread_id=_THREAD_A, transition="added", once=True),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    # 1. Thread resolved -> enqueued & claimed (once=True auto-disables rule with tag_present=True)
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$res-first"))
    assert store.claim_firing(1, now=time.time()) == "claimed"
    assert store.mark_delivered(1, "$sent-first") is True
    disabled = store.get_trigger("tr_once_rearm")
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.disabled_reason == "once_consumed"

    # 2. Tag changes while rule is disabled -> neither enqueues nor mutates checkpoint
    service.observe_state_event(_ROOM_A, _tag_state_source(present=False, event_id="$reopen-while-disabled"))
    cp = store.get_thread_checkpoint("tr_once_rearm")
    assert cp is not None
    assert cp.tag_present is True
    assert cp.state_event_id == "$res-first"
    assert store.list_due_firings() == []


@pytest.mark.asyncio
async def test_thread_tag_live_observation_delivers_when_owner_absent_from_lazy_room_users(tmp_path: Path) -> None:
    """Under $LAZY sync, a joined owner absent from partial room.users MUST still enqueue and deliver."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    # Populate partial room.users containing only another speaker (owner hasn't spoken under $LAZY sync)
    bot.client.rooms[_ROOM_A].users[_OTHER] = SimpleNamespace(user_id=_OTHER)
    assert _OWNER not in bot.client.rooms[_ROOM_A].users

    store.create_trigger(
        _thread_tag_rule("tr_lazy_tag", room_id=_ROOM_A, thread_id=_THREAD_A, transition="added"),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$lazy-resolve-1"))
    assert len(store.list_due_firings()) == 1

    send_mock = AsyncMock(return_value=SimpleNamespace(event_id="$delivered-lazy-1"))
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock):
        await service.drain_due_firings_once()

    send_mock.assert_awaited_once()
    firing = store.get_firing(1)
    assert firing is not None
    assert firing.status == "delivered"
    assert firing.matrix_event_id == "$delivered-lazy-1"


@pytest.mark.asyncio
async def test_drainer_skips_firing_whose_rule_uid_was_replaced(tmp_path: Path) -> None:
    """If a firing row references an old rule_uid after trigger_id reuse, drain_due_firings_once skips it."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()

    bob_rule = _external_event_rule("tr_reused")
    store.create_trigger(bob_rule)
    store.enqueue_firing("tr_reused", "deliv-old-uid", _external_event("deliv-old-uid"))

    # Simulate stale firing carrying a previous rule_uid
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET rule_uid = 'stale-alice-uid' WHERE id = 1")

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    dispatch_mock = AsyncMock()
    with patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", new=dispatch_mock):
        await service.drain_due_firings_once()

    dispatch_mock.assert_not_awaited()
    f = store.get_firing(1)
    assert f is not None
    assert f.status == "skipped"
    assert f.error_message == "rule_replaced"


@pytest.mark.asyncio
async def test_claimed_retry_stops_when_manually_disabled_but_continues_when_once_consumed(tmp_path: Path) -> None:
    """Manually disabled repeating rule stops retrying; auto-consumed once=True rule keeps retrying until delivered."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()

    # Case 1: Repeating rule claimed, enters retry backoff, then manually disabled -> MUST skip retry
    store.create_trigger(_external_event_rule("tr_manual_stop", once=False))
    store.enqueue_firing("tr_manual_stop", "deliv-man-1", _external_event("deliv-man-1"))
    assert store.claim_firing(1, now=time.time()) == "claimed"
    assert store.mark_delivery_retry(1, "transient", now=time.time() - 60.0) == "retrying"
    assert store.set_enabled("tr_manual_stop", False) is not None

    # Case 2: once=True rule claimed (auto-disabled with disabled_reason='once_consumed'), enters retry -> MUST retry!
    store.create_trigger(_external_event_rule("tr_once_retry", once=True))
    store.enqueue_firing("tr_once_retry", "deliv-once-1", _external_event("deliv-once-1"))
    assert store.claim_firing(2, now=time.time()) == "claimed"
    assert store.mark_delivery_retry(2, "transient", now=time.time() - 60.0) == "retrying"

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    dispatched_ids: list[str] = []

    async def _fake_dispatch(*, rule: ReactiveTriggerRecord, **_kwargs: object) -> DispatchResult:
        dispatched_ids.append(rule.trigger_id)
        return DispatchResult(status="delivered", matrix_event_id=f"$sent_{rule.trigger_id}")

    with patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", side_effect=_fake_dispatch):
        await service.drain_due_firings_once()

    assert dispatched_ids == ["tr_once_retry"]
    f_manual = store.get_firing(1)
    f_once = store.get_firing(2)
    assert f_manual is not None
    assert f_manual.status == "skipped"
    assert f_manual.error_message == "disabled_or_expired"
    assert f_once is not None
    assert f_once.status == "delivered"
    assert f_once.matrix_event_id == "$sent_tr_once_retry"


@pytest.mark.asyncio
async def test_pre_triage_target_bot_check_and_policy_rate_limit_avoid_wasting_triage_and_once(tmp_path: Path) -> None:
    """Unavailable target bot defers pending without triage/once consumption; live policy cap skips before triage."""
    config, runtime_paths = _make_config(tmp_path, max_fires_per_hour=1)
    store = ReactiveTriggerStore(runtime_paths)

    # 1. Target bot stopped -> pending once=True triaged rule is deferred without calling triage or consuming once
    stopped_bot = _make_fake_bot(running=False)
    active_bots: dict[str, Any] = {"research": stopped_bot}

    store.create_trigger(
        _external_event_rule(
            "tr_once_wait",
            once=True,
            triage=TriageSpec(instructions="Important?", provider="llm", model="cheap"),
        ),
    )
    store.enqueue_firing("tr_once_wait", "deliv-w1", _external_event("deliv-w1"))

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: active_bots,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="yes"))
    dispatch_mock = AsyncMock(return_value=DispatchResult(status="delivered", matrix_event_id="$delivered-w1"))
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", new=dispatch_mock),
    ):
        await service.drain_due_firings_once()

    triage_mock.assert_not_awaited()
    dispatch_mock.assert_not_awaited()
    f_wait = store.get_firing(1)
    r_wait = store.get_trigger("tr_once_wait")
    assert f_wait is not None
    assert f_wait.status == "pending"
    assert f_wait.next_retry_at is not None
    assert f_wait.error_message == "target_bot_unavailable"
    assert r_wait is not None
    assert r_wait.enabled is True  # once=True was NOT burned!

    # Now target bot comes online and backoff expires -> triage + claim + delivery succeed!
    active_bots["research"] = _make_fake_bot(running=True)
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET next_retry_at = ? WHERE id = 1", (time.time() - 1.0,))
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", new=dispatch_mock),
    ):
        await service.drain_due_firings_once()
    triage_mock.assert_awaited_once()
    assert store.get_firing(1).status == "delivered"  # type: ignore[union-attr]

    # 2. Policy max_fires_per_hour=1 rate-limits a rule created with max_fires_per_hour=60 BEFORE triage
    rule_hi = _external_event_rule(
        "tr_policy_cap",
        once=False,
        triage=TriageSpec(instructions="Check", provider="llm", model="cheap"),
    ).model_copy(update={"max_fires_per_hour": 60})
    store.create_trigger(rule_hi)
    store.enqueue_firing("tr_policy_cap", "deliv-p1", _external_event("deliv-p1"))
    store.enqueue_firing("tr_policy_cap", "deliv-p2", _external_event("deliv-p2"))

    triage_mock.reset_mock()
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.service.dispatch_reactive_trigger", new=dispatch_mock),
    ):
        await service.drain_due_firings_once()

    # Triage ran ONLY for deliv-p1; deliv-p2 was skipped pre-triage due to policy.max_fires_per_hour=1
    assert triage_mock.await_count == 1
    assert store.get_firing(2).status == "delivered"  # type: ignore[union-attr]
    f_rl = store.get_firing(3)
    assert f_rl is not None
    assert f_rl.status == "skipped"
    assert f_rl.error_message == "rate_limited"


@pytest.mark.asyncio
async def test_sync_is_non_blocking_and_drain_survives_sqlite_errors(tmp_path: Path) -> None:
    """sync() schedules thread_tag reconciliation in the background; SQLite errors in status writes don't crash drain."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()

    store.create_trigger(
        _thread_tag_rule("tr_bg_sync", thread_id=_THREAD_A, transition="added"),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    gate = asyncio.Event()
    started = asyncio.Event()

    async def _slow_state(room_id: str) -> nio.RoomGetStateResponse:
        started.set()
        await gate.wait()
        return nio.RoomGetStateResponse([_member_state_source(_OWNER)], room_id)

    bot.client.room_get_state = AsyncMock(side_effect=_slow_state)

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service._poll_interval_seconds = 60.0

    try:
        # sync() MUST return immediately even while room_get_state is blocked on gate
        async with asyncio.timeout(0.5):
            await service.sync(config)
        assert service._reconcile_task is not None
        assert not service._reconcile_task.done()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        gate.set()
        await asyncio.wait_for(service._reconcile_task, timeout=1.0)
        assert _ROOM_A in service._restored_rooms
    finally:
        await service.stop()

    # Transient sqlite3.OperationalError during mark_delivered must not crash drain_due_firings_once
    store.create_trigger(_external_event_rule("tr_sqlite_err"))
    store.enqueue_firing("tr_sqlite_err", "deliv-sq-1", _external_event("deliv-sq-1"))
    with (
        patch(
            "mindroom.reactive_triggers.service.dispatch_reactive_trigger",
            new=AsyncMock(return_value=DispatchResult(status="delivered", matrix_event_id="$sent-sq")),
        ),
        patch.object(store, "mark_delivered", side_effect=sqlite3.OperationalError("database is locked")),
    ):
        await service.drain_due_firings_once()


@pytest.mark.asyncio
async def test_target_access_checked_before_triage_and_claim_preserves_once_rule(tmp_path: Path) -> None:
    """When owner leaves target room, once=True rules (with or without triage) skip without triage or consuming once."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    # Owner is NOT joined in target room _ROOM_B
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse([_member_state_source(_OTHER)], _ROOM_B),
    )

    store.create_trigger(
        _external_event_rule(
            "tr_once_triaged_left",
            once=True,
            triage=TriageSpec(instructions="Important?", provider="llm", model="cheap"),
        ),
    )
    store.create_trigger(_external_event_rule("tr_once_plain_left", once=True))
    store.enqueue_firing("tr_once_triaged_left", "deliv-left-1", _external_event("deliv-left-1"))
    store.enqueue_firing("tr_once_plain_left", "deliv-left-2", _external_event("deliv-left-2"))

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="yes"))
    send_mock = AsyncMock()
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock),
    ):
        await service.drain_due_firings_once()

    triage_mock.assert_not_awaited()
    send_mock.assert_not_awaited()
    for firing_id, trigger_id in ((1, "tr_once_triaged_left"), (2, "tr_once_plain_left")):
        firing = store.get_firing(firing_id)
        rule = store.get_trigger(trigger_id)
        assert firing is not None
        assert firing.status == "skipped"
        assert firing.error_message == "owner_not_joined"
        assert rule is not None
        assert rule.enabled is True
        assert rule.fire_count == 0


@pytest.mark.asyncio
async def test_drain_refreshes_config_per_firing_and_after_triage(tmp_path: Path) -> None:
    """Revoking a subscriber mid-batch skips later firings in the same drain, and mid-triage skips before claim."""
    config_allowed, runtime_paths = _make_config(tmp_path)
    config_revoked, _ = _make_config(
        tmp_path,
        event_sources={
            "github": GitHubEventSourceConfig(
                kind="github",
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["mindroom-ai/mindroom"],
                subscribers=[_OTHER],
            ),
        },
    )
    active_config = [config_allowed]

    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()
    store.create_trigger(_external_event_rule("tr_batch_1"))
    store.create_trigger(_external_event_rule("tr_batch_2"))
    store.enqueue_firing("tr_batch_1", "deliv-b1", _external_event("deliv-b1"))
    store.enqueue_firing("tr_batch_2", "deliv-b2", _external_event("deliv-b2"))

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: active_config[0],
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    async def _send_then_revoke(*_args: object, **_kwargs: object) -> SimpleNamespace:
        active_config[0] = config_revoked
        return SimpleNamespace(event_id="$delivered-b1")

    send_mock = AsyncMock(side_effect=_send_then_revoke)
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock):
        await service.drain_due_firings_once()

    send_mock.assert_awaited_once()
    f1 = store.get_firing(1)
    f2 = store.get_firing(2)
    assert f1 is not None
    assert f1.status == "delivered"
    assert f2 is not None
    assert f2.status == "skipped"
    assert f2.error_message == "source_subscriber_revoked"

    # Also verify revocation during evaluate_triage skips before claim_firing (preserving once=True)
    active_config[0] = config_allowed
    store.create_trigger(
        _external_event_rule(
            "tr_mid_triage",
            once=True,
            triage=TriageSpec(instructions="Check", provider="llm", model="cheap"),
        ),
    )
    store.enqueue_firing("tr_mid_triage", "deliv-mt1", _external_event("deliv-mt1"))

    async def _triage_then_revoke(*_args: object, **_kwargs: object) -> TriageOutcome:
        active_config[0] = config_revoked
        return TriageOutcome(act=True, reason="yes")

    send_mock.reset_mock()
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", side_effect=_triage_then_revoke),
        patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock),
    ):
        await service.drain_due_firings_once()

    send_mock.assert_not_awaited()
    f3 = store.get_firing(3)
    r3 = store.get_trigger("tr_mid_triage")
    assert f3 is not None
    assert f3.status == "skipped"
    assert f3.error_message == "source_subscriber_revoked"
    assert r3 is not None
    assert r3.enabled is True
    assert r3.fire_count == 0


@pytest.mark.asyncio
async def test_corrupt_spec_json_marks_pending_firing_failed_without_blocking_healthy_firings(tmp_path: Path) -> None:
    """A corrupt spec_json row fails immediately instead of staying pending forever and blocking healthy firings."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    bot = _make_fake_bot()

    store.create_trigger(_external_event_rule("tr_corrupt"))
    store.create_trigger(_external_event_rule("tr_healthy"))
    store.enqueue_firing("tr_corrupt", "deliv-corrupt", _external_event("deliv-corrupt"))
    store.enqueue_firing("tr_healthy", "deliv-healthy", _external_event("deliv-healthy"))
    with store._write_transaction() as conn:
        conn.execute("UPDATE reactive_triggers SET spec_json = '{not valid json' WHERE trigger_id = 'tr_corrupt'")

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"research": bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )

    send_mock = AsyncMock(return_value=SimpleNamespace(event_id="$delivered-healthy"))
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock):
        await service.drain_due_firings_once()

    send_mock.assert_awaited_once()
    f_corrupt = store.get_firing(1)
    f_healthy = store.get_firing(2)
    assert f_corrupt is not None
    assert f_corrupt.status == "failed"
    assert f_corrupt.next_retry_at is None
    assert f_corrupt.error_message == "unexpected_error:ValidationError"
    assert f_healthy is not None
    assert f_healthy.status == "delivered"
    assert store.list_due_firings() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_source_responder_auth_revocation_skips_room_message_and_thread_tag(
    tmp_path: Path,
) -> None:
    """Losing responder authorization in a watched source room skips room_message and thread_tag before triage/delivery."""
    config = bind_runtime_paths(
        Config(
            agents={
                "watcher": AgentConfig(
                    display_name="Watcher",
                    rooms=[_ROOM_A],
                    access={"users": [_OTHER]},
                ),
                "research": AgentConfig(
                    display_name="Research",
                    rooms=[_ROOM_B],
                    access={"users": [_OWNER]},
                ),
            },
            models={
                "default": ModelConfig(provider="openai", id="gpt-test"),
                "cheap": ModelConfig(provider="openai", id="gpt-mini"),
            },
            external_trigger_policy=ExternalTriggerPolicyConfig(enabled=True, reactive_enabled=True),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    store = ReactiveTriggerStore(runtime_paths)

    source_bot = _make_fake_bot(agent_name="watcher", joined_rooms=[_ROOM_A])
    target_bot = _make_fake_bot(agent_name="research", joined_rooms=[_ROOM_B])
    # Owner remains joined to both _ROOM_A and _ROOM_B, and still has full access to target agent in _ROOM_B
    room_state_mock = AsyncMock(
        side_effect=lambda room_id: nio.RoomGetStateResponse(
            [_member_state_source(_OWNER), _member_state_source(_OTHER)],
            room_id,
        ),
    )
    source_bot.client.room_get_state = room_state_mock
    target_bot.client.room_get_state = room_state_mock

    msg_rule = _room_message_rule(
        "tr_cross_msg_revoked",
        room_id=_ROOM_A,
        target_room_id=_ROOM_B,
        triage=TriageSpec(instructions="Only urgent", model="cheap", provider="llm"),
    )
    tag_rule = _thread_tag_rule(
        "tr_cross_tag_revoked",
        room_id=_ROOM_A,
        thread_id=_THREAD_A,
    ).model_copy(update={"triage": TriageSpec(instructions="Only urgent", model="cheap", provider="llm")})
    store.create_trigger(msg_rule)
    store.create_trigger(
        tag_rule,
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )
    store.enqueue_firing(
        "tr_cross_msg_revoked",
        "$ev-revoked",
        TriggerEvent(
            source_type="room_message",
            source_id=_ROOM_A,
            kind="m.room.message",
            event_id="$ev-revoked",
            timestamp=time.time(),
            room_id=_ROOM_A,
            actor=_OTHER,
            text="prod alert",
        ),
    )

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"watcher": source_bot, "research": target_bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$tag-revoked"))
    assert len(store.list_due_firings()) == 2

    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="urgent"))
    send_mock = AsyncMock()
    with (
        patch("mindroom.reactive_triggers.service.evaluate_triage", new=triage_mock),
        patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock),
    ):
        await service.drain_due_firings_once()

    triage_mock.assert_not_awaited()
    send_mock.assert_not_awaited()
    f_msg = store.get_firing(1)
    f_tag = store.get_firing(2)
    assert f_msg is not None
    assert f_msg.status == "skipped"
    assert f_msg.error_message == "source_room_not_authorized"
    assert f_tag is not None
    assert f_tag.status == "skipped"
    assert f_tag.error_message == "source_room_not_authorized"


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_source_responder_pending_membership_retries_before_triage(
    tmp_path: Path,
) -> None:
    """Unresolved membership on a watched source room responder schedules retry instead of skipping."""
    config = bind_runtime_paths(
        Config(
            agents={
                "watcher": AgentConfig(
                    display_name="Watcher",
                    rooms=[_ROOM_A],
                    access={"current_room_members": True},
                ),
                "research": AgentConfig(
                    display_name="Research",
                    rooms=[_ROOM_B],
                    access={"users": [_OWNER]},
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-test")},
            external_trigger_policy=ExternalTriggerPolicyConfig(enabled=True, reactive_enabled=True),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    store = ReactiveTriggerStore(runtime_paths)
    source_bot = _make_fake_bot(agent_name="watcher", joined_rooms=[_ROOM_A])
    target_bot = _make_fake_bot(agent_name="research", joined_rooms=[_ROOM_B])
    store.create_trigger(
        _thread_tag_rule("tr_cross_tag_pending", room_id=_ROOM_A, thread_id=_THREAD_A),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id=None, updated_at=time.time()),
    )

    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bots=lambda: {"watcher": source_bot, "research": target_bot},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service.observe_state_event(_ROOM_A, _tag_state_source(present=True, event_id="$tag-pending"))

    send_mock = AsyncMock()
    with patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=send_mock):
        await service.drain_due_firings_once()

    send_mock.assert_not_awaited()
    firing = store.get_firing(1)
    assert firing is not None
    assert firing.status == "pending"
    assert firing.error_message == "source_membership_pending"
    assert firing.next_retry_at is not None

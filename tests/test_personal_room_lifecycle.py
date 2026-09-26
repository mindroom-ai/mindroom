"""Personal-room coordination without constructing a bot or orchestrator."""

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import personal_room_lifecycle
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.main import Config
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from mindroom.matrix.personal_rooms import PersonalRoomRosterMismatchError, PersonalRoomService
from mindroom.matrix.room_member_joins import RoomMemberJoin
from mindroom.matrix.state import MatrixState
from mindroom.personal_room_lifecycle import PersonalRoomLifecycle, PersonalRoomTarget
from tests.conftest import test_runtime_paths


@dataclass
class Coordination:
    """The coordinator's narrow runtime and service boundaries."""

    lifecycle: PersonalRoomLifecycle
    owner: Mock
    local: Mock
    lookup: Mock
    requester: Mock


@pytest.fixture
def coordination(tmp_path: Path) -> Coordination:
    """Wire a real coordinator to transport and service boundaries only."""
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper", "rooms": [], "access": {"users": ["@alice:localhost"]}}},
            "rooms": {"lobby": {}},
            "personal_rooms": {"agent": "helper", "onboarding_rooms": ["lobby"], "commands": ["!personal"]},
        },
    )
    paths = test_runtime_paths(tmp_path)
    state = MatrixState.load(paths)
    state.add_room("lobby", "!lobby:localhost", "#lobby:localhost", "Lobby")
    state.save(paths)
    client = SimpleNamespace(
        user_id="@mindroom_router:localhost",
        joined_members=AsyncMock(
            return_value=nio.JoinedMembersResponse.from_dict(
                {
                    "joined": {"@alice:localhost": {"display_name": "Alice", "avatar_url": None}},
                },
                "!lobby:localhost",
            ),
        ),
        room_resolve_alias=AsyncMock(
            return_value=nio.RoomResolveAliasResponse(
                "#personal_pending:localhost",
                "!pending:localhost",
                [],
            ),
        ),
    )
    runtime = SimpleNamespace(config=config, client=client)
    owner = Mock(spec=PersonalRoomService)
    local = Mock(spec=PersonalRoomService)
    lookup = Mock(return_value=PersonalRoomTarget(owner, first_sync_complete=True))
    requester = Mock(side_effect=lambda event: event.sender)
    lifecycle = PersonalRoomLifecycle("router", runtime, paths, local, lookup, requester)
    return Coordination(lifecycle, owner, local, lookup, requester)


@dataclass
class Clock:
    """Monotonic time as the coordinator reads it, advanced only by the test."""

    now: float = 1000.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Control when failed reconciliation candidates become due again."""
    clock = Clock()
    monkeypatch.setattr("mindroom.personal_room_lifecycle.monotonic", clock)
    return clock


def record_intent(lifecycle: PersonalRoomLifecycle, user: str) -> None:
    """Retain one requester's onboarding intent from the lobby."""
    write_personal_room(
        personal_room_record_path(lifecycle.runtime_paths, "helper", f"@{user}:localhost"),
        PersonalRoomRecord(
            user_id=f"@{user}:localhost",
            alias=f"#personal_{user}:localhost",
            source_room_id="!lobby:localhost",
        ),
    )


def command(body: str = "!personal") -> nio.RoomMessageText:
    """Build one authenticated Matrix text event."""
    return nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$command",
            "sender": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": body},
        },
    )


@pytest.mark.asyncio
async def test_commands_require_exact_self_request_in_onboarding_room(coordination: Coordination) -> None:
    """Only a trusted self-command can reach the selected owner's service."""
    lifecycle = coordination.lifecycle
    room = nio.MatrixRoom("!lobby:localhost", "@mindroom_router:localhost")
    assert not await lifecycle.handle_command(room, command("!personal @bob:localhost"))
    assert not await lifecycle.handle_command(nio.MatrixRoom("!other:localhost", room.own_user_id), command())
    assert await lifecycle.handle_command(room, command("  !personal  "))
    coordination.lookup.assert_called_with("helper")
    coordination.owner.ensure.assert_awaited_once_with(
        "@alice:localhost",
        room.room_id,
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )
    coordination.owner.ensure.reset_mock()
    coordination.requester.side_effect = None
    coordination.requester.return_value = "@bob:localhost"
    assert await lifecycle.handle_command(room, command())
    coordination.owner.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_onboarding_does_not_wait_for_target_first_sync(coordination: Coordination) -> None:
    """Connected targets accept durable events while startup backfill waits for sync."""
    lifecycle = coordination.lifecycle
    coordination.lookup.return_value = PersonalRoomTarget(coordination.owner, first_sync_complete=False)
    lifecycle.runtime.config.personal_rooms.backfill = True
    await lifecycle._reconcile()
    coordination.owner.ensure.assert_not_awaited()
    await lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    coordination.owner.ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_target_keeps_live_trigger_retryable(coordination: Coordination) -> None:
    """An unavailable owner must not consume onboarding intent as successful."""
    coordination.lookup.return_value = None
    with pytest.raises(RuntimeError, match="target is not ready"):
        await coordination.lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    await coordination.lifecycle._reconcile()
    coordination.owner.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_router_never_routes_onboarding(coordination: Coordination) -> None:
    """Only the router selects and invokes the target service."""
    lifecycle = coordination.lifecycle
    lifecycle.agent_name = "helper"
    lifecycle.runtime.config.personal_rooms.backfill = True
    assert not lifecycle.observes_onboarding_joins
    assert not await lifecycle.handle_command(nio.MatrixRoom("!lobby:localhost", "@helper:localhost"), command())
    await lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    await lifecycle._reconcile()
    coordination.lookup.assert_not_called()
    coordination.owner.ensure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "leave", "join"])
async def test_live_membership_leaves_unknown_baseline_to_durable_gate(
    coordination: Coordination,
    previous: str | None,
) -> None:
    """Unknown prior membership may finish a welcome but cannot bypass the baseline gate."""
    room = nio.MatrixRoom("!lobby:localhost", "@mindroom_router:localhost")
    source = {
        "type": "m.room.member",
        "event_id": "$join",
        "sender": "@alice:localhost",
        "state_key": "@alice:localhost",
        "origin_server_ts": 1,
        "content": {"membership": "join"},
    }
    if previous is not None:
        source["unsigned"] = {"prev_content": {"membership": previous}}
    await coordination.lifecycle.member_event(room, nio.RoomMemberEvent.from_dict(source))
    coordination.local.owner_membership_event.assert_awaited_once_with(room.room_id, "@alice:localhost", "join")
    assert coordination.owner.ensure.await_count == (previous == "leave")
    if previous == "leave":
        coordination.owner.ensure.assert_awaited_once_with(
            "@alice:localhost",
            room.room_id,
            coordination.lifecycle.runtime.client,
            reinvite_departed_owner=True,
        )
    join = RoomMemberJoin(room.room_id, "$join", "@alice:localhost", "@alice:localhost", None, None, "join", previous)
    await coordination.lifecycle.baseline_join(join)
    assert coordination.owner.ensure.await_count == (previous != "join")


@pytest.mark.asyncio
async def test_reconciliation_retries_failure_and_resets_on_reload(coordination: Coordination, clock: Clock) -> None:
    """A failed pass retries; a successful pass repeats only after configuration changes."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = [RuntimeError("temporarily unavailable"), None, None]
    await lifecycle._reconcile()
    clock.now += 30
    await lifecycle._reconcile()
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 2
    lifecycle.config_changed()
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 3


@pytest.mark.asyncio
async def test_reconciliation_preserves_cancellation(coordination: Coordination) -> None:
    """Shutdown cancellation still stops a reconciliation pass."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await lifecycle._reconcile()


@pytest.mark.asyncio
async def test_failed_room_retry_does_not_repeat_successful_rooms(coordination: Coordination, clock: Clock) -> None:
    """One rejected room must not repeat successful provisioning for every requester."""
    lifecycle = coordination.lifecycle
    for user in ("alice", "bob"):
        record_intent(lifecycle, user)
    attempted = []
    repaired = False

    async def ensure(user_id: str, *_args: object, **_kwargs: object) -> None:
        attempted.append(user_id)
        if user_id == "@alice:localhost" and not repaired:
            msg = "Personal-room ownership or membership does not match"
            raise RuntimeError(msg)

    coordination.owner.ensure.side_effect = ensure
    await lifecycle._reconcile()
    clock.now += 3600
    await lifecycle._reconcile()
    repaired = True
    clock.now += 3600
    await lifecycle._reconcile()
    clock.now += 3600
    await lifecycle._reconcile()
    assert attempted == ["@alice:localhost", "@bob:localhost", "@alice:localhost", "@alice:localhost"]
    lifecycle.config_changed()
    await lifecycle._reconcile()
    assert attempted[-2:] == ["@alice:localhost", "@bob:localhost"]


@pytest.mark.asyncio
async def test_failing_candidate_backs_off_up_to_an_hour(coordination: Coordination, clock: Clock) -> None:
    """A room that keeps failing is retried after 30 seconds, then twice as long each time, at most hourly."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = RuntimeError("membership mismatch")
    await lifecycle._reconcile()
    for delay in (30, 60, 120, 240, 480, 960, 1920, 3600, 3600):
        attempts = coordination.owner.ensure.await_count
        clock.now += delay - 1
        await lifecycle._reconcile()
        assert coordination.owner.ensure.await_count == attempts
        clock.now += 1
        await lifecycle._reconcile()
        assert coordination.owner.ensure.await_count == attempts + 1
    assert not lifecycle._reconciled


@pytest.mark.asyncio
async def test_backing_off_candidate_does_not_delay_other_requesters(coordination: Coordination, clock: Clock) -> None:
    """New intent is onboarded on the next pass while another requester's room waits out its delay."""
    lifecycle = coordination.lifecycle
    record_intent(lifecycle, "alice")
    attempted = []

    async def ensure(user_id: str, *_args: object, **_kwargs: object) -> None:
        attempted.append(user_id)
        if user_id == "@alice:localhost":
            msg = "Personal-room ownership or membership does not match"
            raise RuntimeError(msg)

    coordination.owner.ensure.side_effect = ensure
    await lifecycle._reconcile()
    record_intent(lifecycle, "bob")
    clock.now += 1
    await lifecycle._reconcile()
    assert attempted == ["@alice:localhost", "@bob:localhost"]
    assert not lifecycle._reconciled


@pytest.mark.asyncio
async def test_success_after_failures_clears_backoff(coordination: Coordination, clock: Clock) -> None:
    """A room that recovers completes reconciliation and keeps no retry delay."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = [RuntimeError("unavailable"), RuntimeError("unavailable"), None]
    await lifecycle._reconcile()
    clock.now += 30
    await lifecycle._reconcile()
    clock.now += 59
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 2
    clock.now += 1
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 3
    assert lifecycle._reconciled
    assert not lifecycle._candidate_backoff


@pytest.mark.asyncio
async def test_reload_retries_backing_off_candidate_immediately(coordination: Coordination, clock: Clock) -> None:
    """A configuration reload may be the fix, so it retries at once and restarts the delays."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = RuntimeError("membership mismatch")
    await lifecycle._reconcile()
    clock.now += 1
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 1
    lifecycle.config_changed()
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 2
    clock.now += 29
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 2
    clock.now += 1
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 3


@pytest.mark.asyncio
async def test_live_triggers_ignore_reconciliation_backoff(coordination: Coordination, clock: Clock) -> None:
    """A requester acting in the lobby is served at once, even while their recorded intent waits."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = [RuntimeError("membership mismatch"), None, None, None]
    await lifecycle._reconcile()
    room = nio.MatrixRoom("!lobby:localhost", "@mindroom_router:localhost")
    assert await lifecycle.handle_command(room, command())
    rejoin = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": "$rejoin",
            "sender": "@alice:localhost",
            "state_key": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"membership": "join"},
            "unsigned": {"prev_content": {"membership": "leave"}},
        },
    )
    await lifecycle.member_event(room, rejoin)
    join = RoomMemberJoin(room.room_id, "$join", "@alice:localhost", "@alice:localhost", None, None, "join", None)
    await lifecycle.baseline_join(join)
    assert coordination.owner.ensure.await_count == 4
    clock.now += 1
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 4


@pytest.mark.asyncio
async def test_repeated_error_logs_its_traceback_once(coordination: Coordination, clock: Clock) -> None:
    """Only the first of the same error in a row carries a traceback; retries report their attempt and next delay."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = RuntimeError("temporarily unavailable")
    with capture_logs() as logs:
        await lifecycle._reconcile()
        clock.now += 30
        await lifecycle._reconcile()
        clock.now += 60
        await lifecycle._reconcile()
    failures = [entry for entry in logs if entry["event"] == "Personal-room reconciliation failed"]
    assert [
        (entry["log_level"], entry.get("exc_info", False), entry["attempt"], entry["retry_in_seconds"])
        for entry in failures
    ] == [("error", True, 1, 30.0), ("warning", False, 2, 60.0), ("warning", False, 3, 120.0)]
    assert [(entry["error_type"], entry["error"]) for entry in failures[1:]] == [
        ("RuntimeError", "temporarily unavailable"),
    ] * 2
    assert {(entry["user_id"], entry["room_id"]) for entry in failures} == {("@alice:localhost", "!lobby:localhost")}


@pytest.mark.asyncio
async def test_a_different_error_logs_a_new_traceback(coordination: Coordination, clock: Clock) -> None:
    """A new kind of failure behind a waiting roster mismatch is a new problem, so it gets its own traceback."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = [
        PersonalRoomRosterMismatchError("!personal:localhost", {"@eve:localhost"}),
        KeyError("content"),
        KeyError("content"),
    ]
    with capture_logs() as logs:
        await lifecycle._reconcile()
        clock.now += 30
        await lifecycle._reconcile()
        clock.now += 60
        await lifecycle._reconcile()
    assert [
        (entry["event"], entry["log_level"], entry.get("exc_info", False))
        for entry in logs
        if entry["log_level"] in {"warning", "error"}
    ] == [
        ("Personal-room imported roster has unattested members", "warning", False),
        ("Personal-room reconciliation failed", "error", True),
        ("Personal-room reconciliation failed", "warning", False),
    ]
    assert logs[-1]["error_type"] == "KeyError"


@pytest.mark.asyncio
@pytest.mark.usefixtures("clock")
async def test_failure_during_reload_leaves_no_stale_delay(coordination: Coordination) -> None:
    """An attempt that fails after a reload cannot delay the new configuration's first retry."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True

    async def ensure(*_args: object, **_kwargs: object) -> None:
        lifecycle.config_changed()
        msg = "membership mismatch"
        raise RuntimeError(msg)

    coordination.owner.ensure.side_effect = ensure
    await lifecycle._reconcile()
    coordination.owner.ensure.side_effect = None
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 2
    assert lifecycle._reconciled


@pytest.mark.asyncio
async def test_imported_roster_mismatch_is_a_warning_without_traceback(
    coordination: Coordination,
    clock: Clock,
) -> None:
    """A room waiting for someone to remove unattested members is reported on each retry, never as a crash."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = PersonalRoomRosterMismatchError(
        "!personal:localhost",
        {"@eve:localhost", "@bob:localhost"},
    )
    with capture_logs() as logs:
        await lifecycle._reconcile()
        clock.now += 30
        await lifecycle._reconcile()
    assert [entry for entry in logs if entry["log_level"] in {"warning", "error"}] == [
        {
            "event": "Personal-room imported roster has unattested members",
            "log_level": "warning",
            "user_id": "@alice:localhost",
            "room_id": "!lobby:localhost",
            "personal_room_id": "!personal:localhost",
            "unexpected_user_ids": ("@bob:localhost", "@eve:localhost"),
            "attempt": attempt,
            "retry_in_seconds": delay,
        }
        for attempt, delay in ((1, 30.0), (2, 60.0))
    ]


@pytest.mark.asyncio
async def test_scheduled_reconciliation_is_single_flight_and_cancellable(coordination: Coordination) -> None:
    """Sync notifications cannot overlap maintenance, and shutdown drains its task."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    attempts = []

    async def ensure(*_args: object, **_kwargs: object) -> None:
        attempts.append(1)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    coordination.owner.ensure.side_effect = ensure
    try:
        lifecycle.schedule_reconciliation()
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(10):
            lifecycle.schedule_reconciliation()
        await lifecycle.cancel_reconciliation(timeout_seconds=2)
        assert cancelled.is_set()
        assert attempts == [1]
        coordination.owner.ensure.side_effect = None
        lifecycle.schedule_reconciliation()
        assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
        assert coordination.owner.ensure.await_count == 2
    finally:
        await lifecycle.cancel_reconciliation(timeout_seconds=2)


@pytest.mark.asyncio
async def test_scheduled_failure_has_cooldown_but_reload_can_retry(
    coordination: Coordination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy sync stream cannot hammer a rejected room; reload resets the delay."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    now = 1000.0
    monkeypatch.setattr("mindroom.personal_room_lifecycle.monotonic", lambda: now)
    coordination.owner.ensure.side_effect = RuntimeError("membership mismatch")
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    now += 1
    for _ in range(10):
        lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    assert coordination.owner.ensure.await_count == 1
    now += 3600
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    assert coordination.owner.ensure.await_count == 2
    lifecycle.config_changed()
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    assert coordination.owner.ensure.await_count == 3


@pytest.mark.asyncio
async def test_target_first_sync_does_not_impose_retry_cooldown(
    coordination: Coordination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first ready notification starts maintenance without waiting for a retry timer."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    monkeypatch.setattr("mindroom.personal_room_lifecycle.monotonic", lambda: 1000.0)
    coordination.lookup.return_value = PersonalRoomTarget(coordination.owner, first_sync_complete=False)
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    coordination.owner.ensure.assert_not_awaited()
    coordination.lookup.return_value = PersonalRoomTarget(coordination.owner, first_sync_complete=True)
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    coordination.owner.ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_record_storage_does_not_block_event_loop(
    coordination: Coordination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event loop progresses while durable room records are being read."""
    lifecycle = coordination.lifecycle
    write_personal_room(
        personal_room_record_path(lifecycle.runtime_paths, "helper", "@alice:localhost"),
        PersonalRoomRecord(
            user_id="@alice:localhost",
            alias="#personal_alice:localhost",
            source_room_id="!lobby:localhost",
        ),
    )
    read = personal_room_lifecycle.read_personal_room
    entered = threading.Event()
    release = threading.Event()
    timed_out = threading.Event()

    def slow_read(path: Path) -> PersonalRoomRecord | None:
        entered.set()
        if not release.wait(timeout=2):
            timed_out.set()
        return read(path)

    monkeypatch.setattr(personal_room_lifecycle, "read_personal_room", slow_read)
    try:
        lifecycle.schedule_reconciliation()
        assert await asyncio.to_thread(entered.wait, 2)
        assert not timed_out.is_set()
    finally:
        release.set()
        assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)


@pytest.mark.asyncio
async def test_reload_during_provisioning_does_not_cache_stale_success(coordination: Coordination) -> None:
    """An old pass cannot satisfy the new configuration or impose its retry cooldown."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    entered = asyncio.Event()
    release = asyncio.Event()

    async def ensure(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    coordination.owner.ensure.side_effect = ensure
    try:
        lifecycle.schedule_reconciliation()
        await asyncio.wait_for(entered.wait(), timeout=2)
        lifecycle.config_changed()
        release.set()
        assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
        lifecycle.schedule_reconciliation()
        assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
        assert coordination.owner.ensure.await_count == 2
    finally:
        await lifecycle.cancel_reconciliation(timeout_seconds=2)


@pytest.mark.asyncio
async def test_backfill_failure_does_not_block_recorded_user(coordination: Coordination) -> None:
    """A lobby membership outage leaves backfill retryable while recorded intent progresses."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    path = personal_room_record_path(lifecycle.runtime_paths, "helper", "@alice:localhost")
    write_personal_room(
        path,
        PersonalRoomRecord(
            user_id="@alice:localhost",
            alias="#personal_alice:localhost",
            source_room_id="!lobby:localhost",
        ),
    )
    lifecycle.runtime.client.joined_members.return_value = nio.JoinedMembersError("unavailable", "M_UNKNOWN")
    await lifecycle._reconcile()
    coordination.owner.ensure.assert_awaited_once()
    lifecycle.runtime.client.joined_members.return_value = nio.JoinedMembersResponse.from_dict(
        {"joined": {"@alice:localhost": {"display_name": "Alice", "avatar_url": None}}},
        "!lobby:localhost",
    )
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 1


@pytest.mark.asyncio
async def test_corrupt_record_does_not_block_other_record(coordination: Coordination) -> None:
    """A malformed retained record cannot starve another requester's intent."""
    lifecycle = coordination.lifecycle
    alice_path = personal_room_record_path(lifecycle.runtime_paths, "helper", "@alice:localhost")
    alice_path.parent.mkdir(parents=True, exist_ok=True)
    alice_path.write_text("not json")
    write_personal_room(
        personal_room_record_path(lifecycle.runtime_paths, "helper", "@bob:localhost"),
        PersonalRoomRecord(
            user_id="@bob:localhost",
            alias="#personal_bob:localhost",
            source_room_id="!lobby:localhost",
        ),
    )
    await lifecycle._reconcile()
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 1
    coordination.owner.ensure.assert_awaited_with(
        "@bob:localhost",
        "!lobby:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_config_object", [False, True])
async def test_reload_during_backfill_keeps_new_onboarding_room_pending(
    coordination: Coordination,
    monkeypatch: pytest.MonkeyPatch,
    same_config_object: bool,
) -> None:
    """A stale pass cannot consume a reload notification received during a network await."""
    lifecycle = coordination.lifecycle
    original = lifecycle.runtime.config
    original.personal_rooms.backfill = True
    original.rooms["new"] = original.rooms["lobby"].model_copy()
    state = MatrixState.load(lifecycle.runtime_paths)
    state.add_room("new", "!new:localhost", "#new:localhost", "New")
    state.save(lifecycle.runtime_paths)
    started = asyncio.Event()
    release = asyncio.Event()
    membership_reads = []

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        membership_reads.append(room_id)
        if room_id == "!lobby:localhost":
            started.set()
            await release.wait()
        return nio.JoinedMembersResponse.from_dict(
            {"joined": {"@alice:localhost": {"display_name": "Alice", "avatar_url": None}}},
            room_id,
        )

    monkeypatch.setattr(lifecycle.runtime.client, "joined_members", joined_members)
    async with asyncio.timeout(2):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(lifecycle._reconcile())
            await started.wait()
            changed = original if same_config_object else original.model_copy(deep=True)
            changed.personal_rooms.onboarding_rooms = ["new"]
            monkeypatch.setattr(lifecycle.runtime, "config", changed)
            lifecycle.config_changed()
            release.set()
    coordination.owner.ensure.assert_not_awaited()
    await lifecycle._reconcile()
    assert membership_reads == ["!lobby:localhost", "!new:localhost"]
    coordination.owner.ensure.assert_awaited_once_with(
        "@alice:localhost",
        "!new:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )


@pytest.mark.asyncio
async def test_disabled_provisioning_retains_only_recorded_ids_for_rejoin(coordination: Coordination) -> None:
    """Unrecorded alias rooms are cleanup exclusions, never automatic rejoin authority."""
    lifecycle = coordination.lifecycle
    lifecycle.agent_name = "helper"
    lifecycle.runtime.config.personal_rooms = None
    for user, room_id in [("alice", "!owned:localhost"), ("bob", None)]:
        user_id = f"@{user}:localhost"
        write_personal_room(
            personal_room_record_path(lifecycle.runtime_paths, "helper", user_id),
            PersonalRoomRecord(
                user_id=user_id,
                alias=f"#personal_{user}:localhost",
                source_room_id="!lobby:localhost",
                room_id=room_id,
            ),
        )
    assert lifecycle.retained_room_ids() == {"!owned:localhost"}
    assert await lifecycle.cleanup_exclusions() == {"!owned:localhost", "!pending:localhost"}
    await lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    await lifecycle._reconcile()
    coordination.owner.ensure.assert_not_awaited()

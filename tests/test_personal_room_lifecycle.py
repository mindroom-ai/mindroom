"""Personal-room coordination without constructing a bot or orchestrator."""

import asyncio
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import personal_room_lifecycle
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.main import Config
from mindroom.config.reload_status import ConfigReloadStatus
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from mindroom.matrix.personal_rooms import (
    PersonalRoomRosterMismatchError,
    PersonalRoomService,
    PersonalRoomValidationError,
)
from mindroom.matrix.room_member_joins import RoomMemberJoin
from mindroom.matrix.state import MatrixState
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.personal_room_lifecycle import PersonalRoomLifecycle, PersonalRoomTarget
from tests.conftest import test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state
from tests.test_personal_agents_config import BASE as PERSONAL_AGENTS_BASE
from tests.test_personal_agents_config import _entry as personal_agent_entry


@dataclass
class Coordination:
    """The coordinator's narrow runtime and service boundaries."""

    lifecycle: PersonalRoomLifecycle
    owner: Mock
    local: Mock
    lookup: Mock


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
    save_matrix_state(state, paths)
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
    lifecycle = PersonalRoomLifecycle("router", runtime, paths, local, lookup, Mock())
    return Coordination(lifecycle, owner, local, lookup)


@dataclass
class TemplateCoordination:
    """A coordinator whose personal_rooms.agent names a template, so each user gets their own agent."""

    lifecycle: PersonalRoomLifecycle
    target: Mock
    lookup: Mock
    provision: AsyncMock
    config_reload: Mock

    def provisioned(self) -> list[tuple[str, str, str, str]]:
        """Return each provisioning request as (template, owner, joining account, source room)."""
        return [
            (
                awaited.kwargs["template"],
                awaited.kwargs["user_id"],
                awaited.kwargs["source_user_id"],
                awaited.kwargs["source_room_id"],
            )
            for awaited in self.provision.await_args_list
        ]


@pytest.fixture
def template_coordination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TemplateCoordination:
    """Wire a real coordinator to stub personal-agent registration and alice's own agent, once it exists."""
    paths = test_runtime_paths(tmp_path)
    state = MatrixState.load(paths)
    state.add_room("lobby", "!lobby:localhost", "#lobby:localhost", "Lobby")
    save_matrix_state(state, paths)
    client = SimpleNamespace(
        user_id="@mindroom_router:localhost",
        joined_members=AsyncMock(
            return_value=nio.JoinedMembersResponse.from_dict(
                {"joined": {"@alice:localhost": {"display_name": "Alice", "avatar_url": None}}},
                "!lobby:localhost",
            ),
        ),
        get_displayname=AsyncMock(return_value=nio.ProfileGetDisplayNameResponse("Alice")),
    )
    runtime = SimpleNamespace(config=Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths), client=client)
    target = Mock(spec=PersonalRoomService)
    lookup = Mock(
        side_effect=lambda name: PersonalRoomTarget(target, first_sync_complete=True) if name == "mind_alice" else None,
    )
    provision = AsyncMock(return_value=("mind_alice", True))
    monkeypatch.setattr(personal_room_lifecycle, "provision_personal_agent", provision)
    lifecycle = PersonalRoomLifecycle(
        "router",
        runtime,
        paths,
        Mock(spec=PersonalRoomService),
        lookup,
        config_reload := Mock(status=ConfigReloadStatus(status="applied")),
    )
    return TemplateCoordination(lifecycle, target, lookup, provision, config_reload)


def set_lobby_members(lifecycle: PersonalRoomLifecycle, *user_ids: str) -> None:
    """Replace who the router sees in the lobby."""
    lifecycle.runtime.client.joined_members.return_value = nio.JoinedMembersResponse.from_dict(
        {"joined": {user_id: {} for user_id in user_ids}},
        "!lobby:localhost",
    )


def add_alices_agent(lifecycle: PersonalRoomLifecycle, source_room_id: str = "!lobby:localhost") -> None:
    """Record alice's own agent in the roster and reload the config that contains it."""
    paths = lifecycle.runtime_paths
    _append_personal_agent_roster_entry(paths, personal_agent_entry("@alice:localhost", source_room_id=source_room_id))
    lifecycle.runtime.config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths)
    lifecycle.config_changed()


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


def lobby_rejoin() -> nio.RoomMemberEvent:
    """Build a lobby leave-to-join event, which asks to re-invite a departed owner."""
    return nio.RoomMemberEvent.from_dict(
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


LOBBY = nio.MatrixRoom("!lobby:localhost", "@mindroom_router:localhost")
_ALICE_JOIN = RoomMemberJoin(LOBBY.room_id, "$join", "@alice:localhost", "@alice:localhost", None, None, "join", None)


@pytest.mark.asyncio
async def test_commands_require_exact_self_request_in_onboarding_room(coordination: Coordination) -> None:
    """Only a trusted self-command can reach the selected owner's service."""
    lifecycle = coordination.lifecycle
    room = nio.MatrixRoom("!lobby:localhost", "@mindroom_router:localhost")
    assert not await lifecycle.handle_command(room, command("!personal @bob:localhost"), "@alice:localhost")
    assert not await lifecycle.handle_command(
        nio.MatrixRoom("!other:localhost", room.own_user_id),
        command(),
        "@alice:localhost",
    )
    assert await lifecycle.handle_command(room, command("  !personal  "), "@alice:localhost")
    coordination.lookup.assert_called_with("helper")
    coordination.owner.ensure.assert_awaited_once_with(
        "@alice:localhost",
        room.room_id,
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )
    coordination.owner.ensure.reset_mock()
    # The event's trusted requester is someone else, as when an entity relays alice's words for bob.
    assert await lifecycle.handle_command(room, command(), "@bob:localhost")
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
    assert not await lifecycle.handle_command(
        nio.MatrixRoom("!lobby:localhost", "@helper:localhost"),
        command(),
        "@alice:localhost",
    )
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
async def test_unfixable_personal_room_settles_its_owners_membership_event(coordination: Coordination) -> None:
    """A raising member callback holds the room's event lane, so the agent would answer nothing there until a repair."""
    lifecycle = coordination.lifecycle
    lifecycle.agent_name = "helper"
    room = nio.MatrixRoom("!personal:localhost", "@mindroom_helper:localhost")
    # A profile change is a join after a join, which the owner's client sends to every room it is in.
    profile_change = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": "$profile",
            "sender": "@alice:localhost",
            "state_key": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"membership": "join", "displayname": "Alice A."},
            "unsigned": {"prev_content": {"membership": "join", "displayname": "Alice"}},
        },
    )
    coordination.local.owner_membership_event.side_effect = PersonalRoomRosterMismatchError(
        room.room_id,
        {"@mindroom_other:localhost"},
    )
    with capture_logs() as logs:
        await lifecycle.member_event(room, profile_change)
    assert [(entry["event"], entry["log_level"]) for entry in logs] == [
        ("Personal-room validation failed for an owner membership event", "warning"),
    ]
    coordination.local.guest_membership_event.assert_awaited_once_with(room, "@alice:localhost", "join")
    # A failure a retry can pass still raises, so the journal keeps the event and retries it.
    coordination.local.owner_membership_event.side_effect = RuntimeError("Personal-room ownership state unavailable")
    with pytest.raises(RuntimeError, match="state unavailable"):
        await lifecycle.member_event(room, profile_change)
    # Every check no retry passes settles, not only the roster mismatch.
    coordination.local.owner_membership_event.side_effect = PersonalRoomValidationError(
        "Personal-room directory must remain private",
    )
    await lifecycle.member_event(room, profile_change)


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
    assert await lifecycle.handle_command(LOBBY, command(), "@alice:localhost")
    await lifecycle.member_event(LOBBY, lobby_rejoin())
    await lifecycle.baseline_join(_ALICE_JOIN)
    assert coordination.owner.ensure.await_count == 4
    clock.now += 1
    await lifecycle._reconcile()
    assert coordination.owner.ensure.await_count == 4


@pytest.mark.asyncio
async def test_failed_live_trigger_is_retried_by_reconciliation_with_its_reinvite_request(
    coordination: Coordination,
) -> None:
    """A lobby rejoin that fails settles, and the next pass retries it even after that requester was reconciled."""
    lifecycle = coordination.lifecycle
    record_intent(lifecycle, "alice")
    await lifecycle._reconcile()
    assert lifecycle._reconciled
    coordination.owner.ensure.side_effect = [RuntimeError("Personal-room invite failed"), None]

    await lifecycle.member_event(LOBBY, lobby_rejoin())
    await lifecycle._reconcile()

    retry = call("@alice:localhost", LOBBY.room_id, lifecycle.runtime.client, reinvite_departed_owner=True)
    assert coordination.owner.ensure.await_args_list[-2:] == [retry, retry]
    assert lifecycle._reconciled


@pytest.mark.asyncio
async def test_failed_live_trigger_without_recorded_intent_is_retried(coordination: Coordination) -> None:
    """A command that fails before its owner records any intent is still retried by the next pass."""
    lifecycle = coordination.lifecycle
    coordination.owner.ensure.side_effect = [RuntimeError("Personal-room onboarding membership unavailable"), None]
    assert await lifecycle.handle_command(LOBBY, command(), "@alice:localhost")

    await lifecycle._reconcile()

    assert coordination.owner.ensure.await_count == 2
    coordination.owner.ensure.assert_awaited_with(
        "@alice:localhost",
        LOBBY.room_id,
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )
    assert lifecycle._reconciled


@pytest.mark.asyncio
async def test_live_trigger_failing_during_a_pass_keeps_reconciliation_pending(coordination: Coordination) -> None:
    """A pass that started before a live trigger failed cannot mark that trigger's retry as done."""
    lifecycle = coordination.lifecycle
    record_intent(lifecycle, "bob")
    attempts = []

    async def ensure(user_id: str, *_args: object, **_kwargs: object) -> None:
        attempts.append(user_id)
        if len(attempts) == 1:
            assert await lifecycle.handle_command(LOBBY, command(), "@alice:localhost")
        elif len(attempts) == 2:
            msg = "Personal-room invite failed"
            raise RuntimeError(msg)

    coordination.owner.ensure.side_effect = ensure
    await lifecycle._reconcile()
    assert not lifecycle._reconciled
    await lifecycle._reconcile()

    assert attempts == ["@bob:localhost", "@alice:localhost", "@alice:localhost"]
    assert lifecycle._reconciled


@pytest.mark.asyncio
@pytest.mark.parametrize(("second_trigger", "retried_reinvite"), [("rejoin", False), ("command", True)])
async def test_successful_live_trigger_retires_only_the_retry_it_covers(
    coordination: Coordination,
    second_trigger: str,
    retried_reinvite: bool,
) -> None:
    """A later rejoin that succeeds satisfies a failed rejoin, so an owner who leaves again is not re-invited.

    A command carries no re-invite request, so its success leaves the failed rejoin's request pending.
    """
    lifecycle = coordination.lifecycle
    record_intent(lifecycle, "alice")
    coordination.owner.ensure.side_effect = [RuntimeError("Personal-room invite failed"), None, None]
    await lifecycle.member_event(LOBBY, lobby_rejoin())
    if second_trigger == "rejoin":
        await lifecycle.member_event(LOBBY, lobby_rejoin())
    else:
        assert await lifecycle.handle_command(LOBBY, command(), "@alice:localhost")

    await lifecycle._reconcile()

    assert coordination.owner.ensure.await_args == call(
        "@alice:localhost",
        LOBBY.room_id,
        lifecycle.runtime.client,
        reinvite_departed_owner=retried_reinvite,
    )
    assert lifecycle._reconciled


@pytest.mark.asyncio
async def test_rejoin_failing_during_its_owners_reconciliation_keeps_the_reinvite_pending(
    coordination: Coordination,
) -> None:
    """An attempt that started without a re-invite request cannot settle one a rejoin added while it ran."""
    lifecycle = coordination.lifecycle
    record_intent(lifecycle, "alice")
    reinvites = []

    async def ensure(*_args: object, reinvite_departed_owner: bool) -> None:
        reinvites.append(reinvite_departed_owner)
        if len(reinvites) == 1:
            await lifecycle.member_event(LOBBY, lobby_rejoin())
        elif len(reinvites) == 2:
            msg = "Personal-room invite failed"
            raise RuntimeError(msg)

    coordination.owner.ensure.side_effect = ensure
    await lifecycle._reconcile()
    assert not lifecycle._reconciled
    await lifecycle._reconcile()

    assert reinvites == [False, True, True]
    assert lifecycle._reconciled


@pytest.mark.asyncio
async def test_live_trigger_preserves_cancellation(coordination: Coordination) -> None:
    """Shutdown cancellation still stops an onboarding-room trigger instead of settling it."""
    coordination.owner.ensure.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await coordination.lifecycle.handle_command(LOBBY, command(), "@alice:localhost")


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
    record_intent(lifecycle, "alice")
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
    record_intent(lifecycle, "alice")
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
    record_intent(lifecycle, "bob")
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
    save_matrix_state(state, lifecycle.runtime_paths)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["first_join", "rejoin"])
async def test_template_join_provisions_and_settles_without_holding_the_lane(
    template_coordination: TemplateCoordination,
    trigger: str,
) -> None:
    """A lobby join creates the user's own agent and settles, so the lobby keeps answering while it starts."""
    lifecycle = template_coordination.lifecycle
    with capture_logs() as logs:
        if trigger == "first_join":
            await lifecycle.baseline_join(_ALICE_JOIN)
        else:
            await lifecycle.member_event(LOBBY, lobby_rejoin())
    assert template_coordination.provisioned() == [("mind", "@alice:localhost", "@alice:localhost", "!lobby:localhost")]
    assert call("mind") not in template_coordination.lookup.call_args_list
    assert lifecycle._retry_candidates == {("@alice:localhost", "!lobby:localhost"): trigger == "rejoin"}
    assert [(entry["event"], entry["log_level"]) for entry in logs] == [("Personal agent is starting", "info")]


@pytest.mark.asyncio
@pytest.mark.parametrize("lobby", ["left", "unavailable"])
async def test_template_join_gets_no_agent_unless_its_sender_is_still_in_the_lobby(
    template_coordination: TemplateCoordination,
    lobby: str,
) -> None:
    """A join seen after its sender left the lobby settles without an agent; an unknown lobby retries it."""
    lifecycle = template_coordination.lifecycle
    if lobby == "left":
        set_lobby_members(lifecycle)
    else:
        lifecycle.runtime.client.joined_members.return_value = nio.JoinedMembersError("unavailable")
    await lifecycle.baseline_join(_ALICE_JOIN)
    template_coordination.provision.assert_not_awaited()
    template_coordination.config_reload.request_reload.assert_not_called()
    retried = {} if lobby == "left" else {("@alice:localhost", "!lobby:localhost"): False}
    assert lifecycle._retry_candidates == retried


@pytest.mark.asyncio
async def test_template_provisioning_failure_backs_off_like_any_failure(
    template_coordination: TemplateCoordination,
    clock: Clock,
) -> None:
    """A registration failure fails only that user's onboarding, which retries after the usual delays."""
    lifecycle = template_coordination.lifecycle
    template_coordination.provision.side_effect = RuntimeError("Failed to register personal agent account")
    with capture_logs() as logs:
        await lifecycle.member_event(LOBBY, lobby_rejoin())
    assert [entry["event"] for entry in logs] == ["Personal-room onboarding trigger failed"]
    await lifecycle._reconcile()
    assert lifecycle._candidate_backoff[("@alice:localhost", "!lobby:localhost")].failures == 1
    clock.now += 29
    await lifecycle._reconcile()
    assert template_coordination.provision.await_count == 2
    clock.now += 1
    await lifecycle._reconcile()
    assert template_coordination.provision.await_count == 3


@pytest.mark.asyncio
async def test_template_reconciliation_ensures_on_the_users_own_agent(
    template_coordination: TemplateCoordination,
) -> None:
    """Once the user's agent is in the config and connected, reconciliation onboards them through it."""
    lifecycle = template_coordination.lifecycle
    add_alices_agent(lifecycle)
    await lifecycle._reconcile()
    template_coordination.lookup.assert_any_call("mind_alice")
    template_coordination.target.ensure.assert_awaited_once_with(
        "@alice:localhost",
        "!lobby:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )
    template_coordination.provision.assert_not_awaited()
    assert lifecycle._reconciled


@pytest.mark.asyncio
async def test_template_reconciliation_retries_intent_recorded_by_each_users_agent(
    template_coordination: TemplateCoordination,
) -> None:
    """A personal room recorded by the user's own agent is retried from its source room, not only the roster's."""
    lifecycle = template_coordination.lifecycle
    add_alices_agent(lifecycle, source_room_id="!retired:localhost")
    write_personal_room(
        personal_room_record_path(lifecycle.runtime_paths, "mind_alice", "@alice:localhost"),
        PersonalRoomRecord(
            user_id="@alice:localhost",
            alias="#personal_alice:localhost",
            source_room_id="!lobby:localhost",
        ),
    )
    await lifecycle._reconcile()
    template_coordination.target.ensure.assert_awaited_once_with(
        "@alice:localhost",
        "!lobby:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
async def test_template_pending_target_does_not_back_off(
    template_coordination: TemplateCoordination,
    connected: bool,
) -> None:
    """A user's agent that is not connected or not synced yet is retried later, without a delay or a warning."""
    lifecycle = template_coordination.lifecycle
    add_alices_agent(lifecycle)
    template_coordination.lookup.side_effect = None
    template_coordination.lookup.return_value = (
        PersonalRoomTarget(template_coordination.target, first_sync_complete=False) if connected else None
    )
    with capture_logs() as logs:
        await lifecycle._reconcile()
    template_coordination.target.ensure.assert_not_awaited()
    assert lifecycle._candidate_backoff == {}
    assert lifecycle._reconciled is False
    assert not [entry for entry in logs if entry["log_level"] in {"warning", "error"}]


@pytest.mark.asyncio
async def test_target_ready_allows_immediate_reconciliation(template_coordination: TemplateCoordination) -> None:
    """A user's agent finishing its first sync onboards its owner at once instead of after the retry delay."""
    lifecycle = template_coordination.lifecycle
    add_alices_agent(lifecycle)
    lifecycle._reconciled = True
    lifecycle._next_reconciliation_at = float("inf")
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    template_coordination.target.ensure.assert_not_awaited()
    lifecycle.target_ready()
    assert lifecycle._next_reconciliation_at == 0.0
    assert lifecycle._reconciled is False
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    template_coordination.target.ensure.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_in", ["matrix_state", "roster"])
async def test_template_join_by_an_unpublished_mindroom_account_settles_without_provisioning(
    template_coordination: TemplateCoordination,
    recorded_in: str,
) -> None:
    """An agent's account that joins the lobby before a reload makes it known never gets an agent of its own."""
    lifecycle = template_coordination.lifecycle
    paths = lifecycle.runtime_paths
    if recorded_in == "matrix_state":
        state = MatrixState.load(paths)
        state.add_account("agent_mind_bob", "mindroom_mind_bob", None, domain="localhost")
        save_matrix_state(state, paths)
    else:
        _append_personal_agent_roster_entry(paths, personal_agent_entry("@bob:localhost"))
    bot_id = "@mindroom_mind_bob:localhost"
    join = RoomMemberJoin(LOBBY.room_id, "$join", bot_id, bot_id, None, None, "join", None)
    with capture_logs() as logs:
        await lifecycle.baseline_join(join)
    template_coordination.provision.assert_not_awaited()
    assert lifecycle._retry_candidates == {}
    assert not [entry for entry in logs if entry["log_level"] in {"warning", "error"}]


@pytest.mark.asyncio
async def test_template_bridge_alias_onboards_onto_its_persons_agent(
    template_coordination: TemplateCoordination,
) -> None:
    """A bridge alias gets its person's agent, which then serves the alias account's own personal room."""
    lifecycle = template_coordination.lifecycle
    paths = lifecycle.runtime_paths
    data = deepcopy(PERSONAL_AGENTS_BASE)
    data["authorization"] = {"aliases": {"@alice:localhost": ["@bridge_alice:localhost"]}}
    lifecycle.runtime.config = Config.validate_with_runtime(data, paths)
    alias = "@bridge_alice:localhost"
    set_lobby_members(lifecycle, alias)
    await lifecycle.baseline_join(RoomMemberJoin(LOBBY.room_id, "$join", alias, alias, None, None, "join", None))
    assert template_coordination.provisioned() == [("mind", "@alice:localhost", alias, "!lobby:localhost")]

    _append_personal_agent_roster_entry(
        paths,
        personal_agent_entry("@alice:localhost", source_room_id="!lobby:localhost"),
    )
    lifecycle.runtime.config = Config.validate_with_runtime(data, paths)
    lifecycle.config_changed()
    await lifecycle._reconcile()
    template_coordination.target.ensure.assert_any_await(
        alias,
        "!lobby:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )


@pytest.mark.asyncio
async def test_template_alias_onboarding_survives_a_restart(template_coordination: TemplateCoordination) -> None:
    """After a restart, reconciliation retries the alias whose join onboarded the owner, not the owner's own ID."""
    lifecycle = template_coordination.lifecycle
    paths = lifecycle.runtime_paths
    alias = "@bridge_alice:localhost"
    data = deepcopy(PERSONAL_AGENTS_BASE)
    data["authorization"] = {"aliases": {"@alice:localhost": [alias]}}
    _append_personal_agent_roster_entry(
        paths,
        personal_agent_entry("@alice:localhost", source_room_id="!lobby:localhost", source_user_id=alias),
    )
    lifecycle.runtime.config = Config.validate_with_runtime(data, paths)
    lifecycle.config_changed()
    await lifecycle._reconcile()
    template_coordination.target.ensure.assert_awaited_once_with(
        alias,
        "!lobby:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )


@pytest.mark.asyncio
async def test_one_users_provisioning_failure_does_not_stop_another_in_the_same_pass(
    template_coordination: TemplateCoordination,
) -> None:
    """A registration failure for one user still lets the next candidate in that pass onboard."""
    lifecycle = template_coordination.lifecycle
    add_alices_agent(lifecycle)
    set_lobby_members(lifecycle, "@aaron:localhost", "@alice:localhost")
    # Sorted before alice, so her candidate runs after the failure.
    lifecycle._queue_retry(("@aaron:localhost", "!lobby:localhost"), reinvite_departed_owner=False)
    template_coordination.provision.side_effect = RuntimeError("Failed to register personal agent account")
    await lifecycle._reconcile()
    assert template_coordination.provisioned() == [("mind", "@aaron:localhost", "@aaron:localhost", "!lobby:localhost")]
    template_coordination.target.ensure.assert_awaited_once_with(
        "@alice:localhost",
        "!lobby:localhost",
        lifecycle.runtime.client,
        reinvite_departed_owner=False,
    )
    assert lifecycle._candidate_backoff[("@aaron:localhost", "!lobby:localhost")].failures == 1


@pytest.mark.asyncio
async def test_target_ready_during_a_pass_starts_the_next_pass_at_once(
    template_coordination: TemplateCoordination,
    clock: Clock,
) -> None:
    """A user's agent that finishes its first sync while a pass runs is onboarded right after that pass."""
    lifecycle = template_coordination.lifecycle
    add_alices_agent(lifecycle)
    target = template_coordination.target
    synced = False

    def lookup(name: str) -> PersonalRoomTarget | None:
        nonlocal synced
        if name != "mind_alice":
            return None
        if synced:
            return PersonalRoomTarget(target, first_sync_complete=True)
        # The pass sees the agent unsynced, and its first sync completes before the pass ends.
        synced = True
        lifecycle.target_ready()
        return PersonalRoomTarget(target, first_sync_complete=False)

    template_coordination.lookup.side_effect = lookup
    lifecycle.schedule_reconciliation()
    assert await wait_for_background_tasks(timeout=2, owner=lifecycle.runtime)
    assert clock.now == 1000.0
    target.ensure.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "display_name"),
    [
        (nio.ProfileGetDisplayNameResponse("Alice A."), "Alice A."),
        (nio.ProfileGetDisplayNameError("missing", "M_NOT_FOUND"), None),
    ],
)
async def test_a_joining_users_agent_is_provisioned_under_their_display_name_and_reloaded(
    template_coordination: TemplateCoordination,
    profile: object,
    display_name: str | None,
) -> None:
    """A lobby join with a template target creates that user's agent under their display name, then reloads."""
    lifecycle = template_coordination.lifecycle
    lifecycle.runtime.client.get_displayname = AsyncMock(return_value=profile)

    await lifecycle.baseline_join(_ALICE_JOIN)

    lifecycle.runtime.client.get_displayname.assert_awaited_once_with("@alice:localhost")
    template_coordination.provision.assert_awaited_once_with(
        lifecycle.runtime.config,
        lifecycle.runtime_paths,
        template="mind",
        user_id="@alice:localhost",
        user_display_name=display_name,
        source_user_id="@alice:localhost",
        source_room_id="!lobby:localhost",
    )
    template_coordination.config_reload.request_reload.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "status", "reloads"),
    [(True, "pending", True), (False, "pending", False), (False, "applied", True), (False, "failed", True)],
)
async def test_reprovisioning_never_supersedes_a_queued_reload(
    template_coordination: TemplateCoordination,
    changed: bool,
    status: str,
    reloads: bool,
) -> None:
    """An agent whose roster entry already existed asks for a reload only when none is queued, so retries cannot restart its wait."""
    template_coordination.provision.return_value = ("mind_alice", changed)
    template_coordination.config_reload.status = ConfigReloadStatus(status=status)

    await template_coordination.lifecycle.baseline_join(_ALICE_JOIN)

    assert template_coordination.config_reload.request_reload.called is reloads


@pytest.mark.asyncio
async def test_one_users_agent_is_provisioned_one_call_at_a_time(template_coordination: TemplateCoordination) -> None:
    """A second call for a user being provisioned returns at once instead of registering again; other users proceed."""
    lifecycle = template_coordination.lifecycle
    release = asyncio.Event()
    users: list[str] = []

    async def provision(*_args: object, user_id: str, **_kwargs: object) -> tuple[str, bool]:
        users.append(user_id)
        await release.wait()
        return "mind_x", True

    template_coordination.provision.side_effect = provision
    first = asyncio.create_task(
        lifecycle._provision_personal_agent(
            "mind",
            user_id="@alice:localhost",
            source_user_id="@alice:localhost",
            source_room_id="!lobby:localhost",
        ),
    )
    await asyncio.sleep(0)
    await asyncio.wait_for(
        lifecycle._provision_personal_agent(
            "mind",
            user_id="@alice:localhost",
            source_user_id="@bridge_alice:localhost",
            source_room_id="!other:localhost",
        ),
        timeout=1,
    )
    other = asyncio.create_task(
        lifecycle._provision_personal_agent(
            "mind",
            user_id="@bob:localhost",
            source_user_id="@bob:localhost",
            source_room_id="!lobby:localhost",
        ),
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, other)

    assert users == ["@alice:localhost", "@bob:localhost"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", [RuntimeError("registration failed"), asyncio.CancelledError()])
async def test_an_interrupted_provisioning_lets_the_next_request_for_that_user_run(
    template_coordination: TemplateCoordination,
    interruption: BaseException,
) -> None:
    """A provisioning call that fails or is cancelled requests no reload and no longer blocks that user's next request."""
    lifecycle = template_coordination.lifecycle
    template_coordination.provision.side_effect = [interruption, ("mind_alice", True)]

    with pytest.raises(type(interruption)):
        await lifecycle._provision_personal_agent(
            "mind",
            user_id="@alice:localhost",
            source_user_id="@alice:localhost",
            source_room_id="!lobby:localhost",
        )
    template_coordination.config_reload.request_reload.assert_not_called()
    await lifecycle._provision_personal_agent(
        "mind",
        user_id="@alice:localhost",
        source_user_id="@alice:localhost",
        source_room_id="!lobby:localhost",
    )

    assert template_coordination.provision.await_count == 2
    template_coordination.config_reload.request_reload.assert_called_once_with()

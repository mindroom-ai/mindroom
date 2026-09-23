"""Personal-room coordination without constructing a bot or orchestrator."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from mindroom.matrix.personal_rooms import PersonalRoomService
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
    await lifecycle.reconcile()
    coordination.owner.ensure.assert_not_awaited()
    await lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    coordination.owner.ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_target_keeps_live_trigger_retryable(coordination: Coordination) -> None:
    """An unavailable owner must not consume onboarding intent as successful."""
    coordination.lookup.return_value = None
    with pytest.raises(RuntimeError, match="target is not ready"):
        await coordination.lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    await coordination.lifecycle.reconcile()
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
    await lifecycle.reconcile()
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
async def test_reconciliation_retries_failure_and_resets_on_reload(coordination: Coordination) -> None:
    """A failed pass retries; a successful pass repeats only after configuration changes."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = [RuntimeError("temporarily unavailable"), None, None]
    await lifecycle.reconcile()
    await lifecycle.reconcile()
    await lifecycle.reconcile()
    assert coordination.owner.ensure.await_count == 2
    lifecycle.config_changed()
    await lifecycle.reconcile()
    assert coordination.owner.ensure.await_count == 3


@pytest.mark.asyncio
async def test_reconciliation_preserves_cancellation(coordination: Coordination) -> None:
    """Shutdown cancellation still stops a reconciliation pass."""
    lifecycle = coordination.lifecycle
    lifecycle.runtime.config.personal_rooms.backfill = True
    coordination.owner.ensure.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await lifecycle.reconcile()


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
    await lifecycle.reconcile()
    coordination.owner.ensure.assert_awaited_once()
    lifecycle.runtime.client.joined_members.return_value = nio.JoinedMembersResponse.from_dict(
        {"joined": {"@alice:localhost": {"display_name": "Alice", "avatar_url": None}}},
        "!lobby:localhost",
    )
    await lifecycle.reconcile()
    assert coordination.owner.ensure.await_count == 2


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
    await lifecycle.reconcile()
    await lifecycle.reconcile()
    assert coordination.owner.ensure.await_count == 2
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
            tasks.create_task(lifecycle.reconcile())
            await started.wait()
            changed = original if same_config_object else original.model_copy(deep=True)
            changed.personal_rooms.onboarding_rooms = ["new"]
            monkeypatch.setattr(lifecycle.runtime, "config", changed)
            lifecycle.config_changed()
            release.set()
    coordination.owner.ensure.assert_not_awaited()
    await lifecycle.reconcile()
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
    await lifecycle.reconcile()
    coordination.owner.ensure.assert_not_awaited()

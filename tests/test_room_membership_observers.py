"""Tests for reply-membership tracking through one pinned observer account per room."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.event_journal import (
    DepartureSource,
    EventClass,
    EventKind,
    InboundEvent,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
)
from mindroom.matrix.state import MatrixState
from mindroom.room_steward import membership_observers, pick_steward
from tests.access_schema_support import joined_members_response
from tests.matrix_state_helpers import save_matrix_state
from tests.test_agent_reply_membership_sync import _fleet, _ingestion_admission

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.bot import AgentBot

_A = "@mindroom_a:example.com"
_B = "@mindroom_b:example.com"
_ALICE = "@alice:example.com"
_BOB = "@bob:example.com"
_ROOM = "!adhoc:example.com"
_OTHER = "!other:example.com"


@dataclass
class _Account:
    """One MindRoom account's Matrix client: the rooms it has joined and the members its reads answer for each."""

    user_id: str
    answers: dict[str, tuple[str, ...]]
    joined_rooms_fails: bool = False
    rooms: dict[str, nio.MatrixRoom] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    # A member read waits here once set, after ``reading`` reports that it began.
    hold: asyncio.Event | None = None
    reading: asyncio.Event = field(default_factory=asyncio.Event)

    async def joined_rooms(self) -> nio.JoinedRoomsResponse | nio.JoinedRoomsError:
        if self.joined_rooms_fails:
            return nio.JoinedRoomsError("unavailable")
        return nio.JoinedRoomsResponse(rooms=list(self.answers))

    async def joined_members(self, room_id: str) -> nio.JoinedMembersResponse:
        self.reads.append(room_id)
        self.reading.set()
        if self.hold is not None:
            await self.hold.wait()
        return joined_members_response(room_id, *self.answers[room_id])


def _config(tmp_path: Path, *, grant_room: bool = False, second_key: bool = False) -> tuple[Config, RuntimePaths]:
    """Build one agent whose reply access is the members of the room it is asked in.

    With ``grant_room`` its access is the members of the managed room ``project`` instead, which is ``_ROOM``.
    With ``second_key`` the managed room ``mirror`` grants too, and is ``_ROOM`` as well.
    """
    grant_keys = ["project", "mirror"] if second_key else ["project"] if grant_room else []
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://example.com"},
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "rooms": grant_keys,
                    "access": {"current_room_members": not grant_keys, "members_of_rooms": grant_keys},
                },
            },
        },
        runtime_paths,
    )
    if grant_keys:
        state = MatrixState.load(runtime_paths=runtime_paths)
        for key in grant_keys:
            state.add_room(key, _ROOM, f"#{key}:example.com", key.title())
        save_matrix_state(state, runtime_paths)
    return config, runtime_paths


def _by_power(power: Mapping[str, float]) -> Callable[[str, Sequence[str]], str | None]:
    """Return the steward rule over fixed power levels, 0 for an account ``power`` leaves out."""
    return lambda _room_id, user_ids: pick_steward((user_id, power.get(user_id, 0)) for user_id in user_ids)


async def _refresh(
    index: AgentReplyMembershipIndex,
    config: Config,
    runtime_paths: RuntimePaths,
    *accounts: _Account,
    power: Mapping[str, float] | None = None,
) -> None:
    await index.refresh(config, runtime_paths, lambda: (_clients(*accounts), _by_power(power or {}), True))


def _clients(*accounts: _Account) -> list[nio.AsyncClient]:
    return cast("list[nio.AsyncClient]", list(accounts))


async def _reached(barrier: asyncio.Event) -> None:
    """Wait for a test barrier, failing instead of hanging when the code under test never reaches it."""
    await asyncio.wait_for(barrier.wait(), 5)


def _observers(index: AgentReplyMembershipIndex) -> dict[str | None, str | None]:
    """Return each ready row's room ID and the account that observes it."""
    return {room.room_id: room.observer_user_id for room in index.snapshot.rooms if room.ready}


def _member_event(user_id: str, membership: str, *, sender: str | None = None) -> nio.RoomMemberEvent:
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": f"${membership}-{user_id}",
            "sender": sender or user_id,
            "state_key": user_id,
            "origin_server_ts": 1,
            "content": {"membership": membership},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    return event


def _sync(index: AgentReplyMembershipIndex) -> AgentReplyMembershipSync:
    return AgentReplyMembershipSync(index, lambda: ())


_OBSERVER_STATES = {None: "closed", False: "armed", True: "open"}


def _enter(membership_sync: AgentReplyMembershipSync, config: Config, state: str) -> None:
    """Bring account A's receive loop into one eligibility state through the events that lead there."""
    if state != "closed":
        membership_sync.observer_loop_started(config, _A)
    if state == "open":
        membership_sync.observer_frame_settled(_A)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "event", "next_state", "invalidated"),
    [
        ("closed", "loop_started", "armed", False),
        ("closed", "frame_settled", "closed", False),
        ("closed", "closed", "closed", False),
        ("armed", "loop_started", "armed", False),
        ("armed", "frame_settled", "open", False),
        ("armed", "closed", "closed", False),
        ("open", "loop_started", "armed", True),
        ("open", "frame_settled", "open", False),
        ("open", "closed", "closed", True),
    ],
)
async def test_observer_eligibility_moves_only_by_its_table(
    tmp_path: Path,
    state: str,
    event: str,
    next_state: str,
    *,
    invalidated: bool,
) -> None:
    """Every (state, event) pair: the next state, and rows and epoch change only when an open account closes."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    _enter(membership_sync, config, state)
    await _refresh(index, config, runtime_paths, _Account(_A, {_ROOM: (_A, _ALICE)}))
    assert _observers(index) == {_ROOM: _A}
    epoch = index._epoch
    membership_sync._refresh_retry_at = float("inf")

    if event == "loop_started":
        membership_sync.observer_loop_started(config, _A)
    elif event == "frame_settled":
        membership_sync.observer_frame_settled(_A)
    else:
        membership_sync.observer_closed(config, _A, reason="sync_failure")

    assert _OBSERVER_STATES[membership_sync._observer_open.get(_A)] == next_state
    assert membership_sync.observer_is_open(_A) is (next_state == "open")
    assert (_observers(index) == {}, index._epoch != epoch) == (invalidated, invalidated)
    # Only the edge that opens an account asks for discovery at once, and a lost observer for a rebuild.
    assert (membership_sync._refresh_retry_at == 0.0) is (invalidated or (state, next_state) == ("armed", "open"))
    # While a loop is armed, its account's rooms are in no census, so no room counts as proven absent.
    assert index.knows_joined_rooms() is (not invalidated and event != "loop_started")


@pytest.mark.asyncio
async def test_room_without_owner_gets_ready_row_from_its_steward(tmp_path: Path) -> None:
    """A room only agents have joined is read through its steward, the joined account with the most power."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()

    await _refresh(index, config, runtime_paths, a, b, power={_B: 50})

    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert not index.is_current_room_member(_BOB, _ROOM, config, runtime_paths)
    assert not index.needs_refresh(config)
    assert _observers(index) == {_ROOM: _B}
    assert (a.reads, b.reads) == ([], [_ROOM])


@pytest.mark.asyncio
async def test_ready_row_records_its_observer(tmp_path: Path) -> None:
    """A configured grant room's row names the one account whose read built it, the lowest ID at equal power."""
    config, runtime_paths = _config(tmp_path, grant_room=True)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()

    await _refresh(index, config, runtime_paths, b, a)

    (row,) = index.snapshot.rooms
    assert (row.room_key, row.ready, row.observer_user_id) == ("project", True, _A)
    assert index.is_allowed(_ALICE, ["project"], config, runtime_paths)
    assert (a.reads, b.reads) == ([_ROOM], [])


@pytest.mark.asyncio
async def test_non_observer_join_event_is_ignored(tmp_path: Path) -> None:
    """Another account's copy of a join must not grant: only the observer's sync is ordered against its own read."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()
    await _refresh(index, config, runtime_paths, _Account(_A, {_ROOM: (_A, _B)}), _Account(_B, {_ROOM: (_A, _B)}))

    changed = index.apply_member_event(config, _ROOM, _member_event(_ALICE, "join"), control_user_id=_B)

    assert not changed
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)


@pytest.mark.asyncio
async def test_non_observer_leave_event_is_ignored(tmp_path: Path) -> None:
    """Another account's copy of a leave may trail a re-join the observer already applied, so it must not deny."""
    config, runtime_paths = _config(tmp_path)
    members = (_A, _B, _ALICE)
    index = AgentReplyMembershipIndex()
    await _refresh(index, config, runtime_paths, _Account(_A, {_ROOM: members}), _Account(_B, {_ROOM: members}))

    changed = index.apply_member_event(config, _ROOM, _member_event(_ALICE, "leave"), control_user_id=_B)

    assert not changed
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert not index.needs_refresh(config)


@pytest.mark.asyncio
async def test_observer_kick_event_revokes_grant(tmp_path: Path) -> None:
    """A kick the observer's sync delivers ends the kicked person's access at once."""
    config, runtime_paths = _config(tmp_path)
    members = (_A, _B, _ALICE)
    index = AgentReplyMembershipIndex()
    await _refresh(index, config, runtime_paths, _Account(_A, {_ROOM: members}), _Account(_B, {_ROOM: members}))

    changed = index.apply_member_event(config, _ROOM, _member_event(_ALICE, "leave", sender=_BOB), control_user_id=_A)

    assert changed
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("departure", ["admitted", "reported", "local"])
async def test_observer_departure_fails_room_closed_then_refresh_repins(tmp_path: Path, departure: str) -> None:
    """A room whose observer left denies until the next refresh reads it through an account that is still joined.

    The account's own leave command reaches its journal as a ``local`` lifecycle record with no timeline event.
    """
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    await _refresh(index, config, runtime_paths, a, b)
    assert _observers(index) == {_ROOM: _A}

    if departure == "admitted":
        await membership_sync.apply_live_transition(config, _ROOM, _member_event(_A, "leave"), control_user_id=_A)
    else:
        membership_sync.pre_admit_ingestion(
            config,
            runtime_paths,
            _ingestion_admission(
                IngestionRecordDisposition.ROOM_LIFECYCLE,
                source=DepartureSource(departure),
                room_id=_ROOM,
                previous_membership="join",
                membership="leave",
            ),
            None,
            observer_user_id=_A,
        )

    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.needs_refresh(config)

    del a.answers[_ROOM]
    b.answers[_ROOM] = (_B, _ALICE, _BOB)
    await _refresh(index, config, runtime_paths, a, b)

    assert _observers(index) == {_ROOM: _B}
    assert index.is_current_room_member(_BOB, _ROOM, config, runtime_paths)
    assert (a.reads, b.reads) == ([_ROOM], [_ROOM])
    assert not index.needs_refresh(config)


async def _two_observed_rooms(
    tmp_path: Path,
) -> tuple[AgentReplyMembershipIndex, AgentReplyMembershipSync, Config, RuntimePaths]:
    """Return an index where account A observes ``_ROOM`` and account B observes ``_OTHER``, Alice in both."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()
    a = _Account(_A, {_ROOM: (_A, _ALICE)})
    b = _Account(_B, {_OTHER: (_B, _ALICE)})
    await _refresh(index, config, runtime_paths, a, b)
    assert _observers(index) == {_ROOM: _A, _OTHER: _B}
    return index, _sync(index), config, runtime_paths


@pytest.mark.asyncio
async def test_observer_sync_gap_unreadies_only_its_rooms(tmp_path: Path) -> None:
    """History one account's sync lost voids the rooms it observes; a room another account observes keeps granting."""
    index, membership_sync, config, runtime_paths = await _two_observed_rooms(tmp_path)

    membership_sync.pre_admit_ingestion(
        config,
        runtime_paths,
        _ingestion_admission(IngestionRecordDisposition.HISTORY_LOSS, room_id=_ROOM),
        None,
        observer_user_id=_A,
    )

    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.is_current_room_member(_ALICE, _OTHER, config, runtime_paths)
    assert index.needs_refresh(config)
    # The account may have joined rooms meanwhile, so a source in an unknown room waits instead of being denied.
    assert not index.knows_joined_rooms()
    assert index.grants_pending(config, joined_rooms=[], current_room_id="!new:example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(("reporter", "still_ready"), [(_A, False), (_B, True)])
async def test_recovered_membership_unreadies_a_room_only_for_its_observer(
    tmp_path: Path,
    reporter: str,
    *,
    still_ready: bool,
) -> None:
    """A membership event recovered after a gap voids its room when the room's observer recovered it, else nothing."""
    index, membership_sync, config, runtime_paths = await _two_observed_rooms(tmp_path)
    recovered = _ingestion_admission(
        IngestionRecordDisposition.ROOM_LIFECYCLE,
        event=InboundEvent(
            event_id="$member",
            room_id=_ROOM,
            thread_id=None,
            kind=EventKind.ROOM_LIFECYCLE,
            event_class=EventClass.CONTEXT_ONLY,
            sender=_ALICE,
            origin_server_ts=1,
            source={},
        ),
    )

    membership_sync.pre_admit_ingestion(
        config,
        runtime_paths,
        recovered,
        nio.TimelineEventProvenance.RECOVERED,
        observer_user_id=reporter,
    )

    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths) is still_ready
    assert index.is_current_room_member(_ALICE, _OTHER, config, runtime_paths)


@pytest.mark.asyncio
async def test_observer_sync_restart_unreadies_only_its_rooms(tmp_path: Path) -> None:
    """A restarted sync loop may have missed membership changes, in the rooms its account observes only."""
    index, membership_sync, config, runtime_paths = await _two_observed_rooms(tmp_path)

    membership_sync.invalidate_observer(config, _A, reason="sync_loop_started")

    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.is_current_room_member(_ALICE, _OTHER, config, runtime_paths)
    assert index.needs_refresh(config)
    assert not index.knows_joined_rooms()


@pytest.mark.asyncio
async def test_observer_stop_during_refresh_voids_its_rows(tmp_path: Path) -> None:
    """A refresh that read a room through an account that stopped meanwhile must not publish that room as ready."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)}, hold=asyncio.Event())
    b = _Account(_B, {_OTHER: (_B, _ALICE)})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    await _refresh(index, config, runtime_paths, b)
    b.answers[_ROOM] = (_A, _B, _ALICE)
    refresh = asyncio.create_task(_refresh(index, config, runtime_paths, a, b, power={_A: 50}))
    await _reached(a.reading)

    # No published row names A yet, so the invalidation must void the refresh that holds A's answers.
    membership_sync.invalidate_observer(config, _A, reason="observer_stopping")
    assert a.hold is not None
    a.hold.set()
    await asyncio.wait_for(refresh, 5)

    assert _observers(index) == {_OTHER: _B}
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.needs_refresh(config)

    await _refresh(index, config, runtime_paths, b)

    assert _observers(index) == {_OTHER: _B, _ROOM: _B}
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)


@pytest.mark.asyncio
async def test_refresh_queued_behind_another_reads_only_through_the_accounts_observing_when_it_starts(
    tmp_path: Path,
) -> None:
    """A refresh that waited for another one picks its accounts once it runs, never one that stopped while it waited.

    The stopped account's client still answers during its drain, so its answers would publish a row nobody observes.
    """
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)}, hold=asyncio.Event())
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    observing = [a, b]

    def observers() -> tuple[list[nio.AsyncClient], Callable[[str, Sequence[str]], str | None], bool]:
        return _clients(*observing), _by_power({_A: 50}), True

    blocked = asyncio.create_task(index.refresh(config, runtime_paths, observers))
    await _reached(a.reading)
    queued = asyncio.create_task(index.refresh(config, runtime_paths, observers))
    await asyncio.sleep(0)

    observing.remove(a)
    membership_sync.invalidate_observer(config, _A, reason="observer_stopping")
    assert a.hold is not None
    a.hold.set()
    await asyncio.wait_for(asyncio.gather(blocked, queued), 5)

    assert _observers(index) == {_ROOM: _B}
    assert (a.reads, b.reads) == ([_ROOM], [_ROOM])
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert not index.needs_refresh(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(DepartureSource))
async def test_account_reporting_its_departure_during_the_refresh_that_pins_a_room_to_it_voids_the_room(
    tmp_path: Path,
    source: DepartureSource,
) -> None:
    """A refresh moving a room to an account whose sync reports it left meanwhile must not publish the room ready."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    await _refresh(index, config, runtime_paths, a, b, power={_B: 50})
    membership_sync.invalidate_observer(config, _B, reason="observer_stopping")
    a.hold = asyncio.Event()
    refresh = asyncio.create_task(_refresh(index, config, runtime_paths, a))
    await _reached(a.reading)

    # No published row names A yet, so only the refresh in flight can apply this.
    membership_sync.pre_admit_ingestion(
        config,
        runtime_paths,
        _ingestion_admission(
            IngestionRecordDisposition.ROOM_LIFECYCLE,
            source=source,
            room_id=_ROOM,
            previous_membership="join",
            membership="leave",
        ),
        None,
        observer_user_id=_A,
    )
    a.hold.set()
    await asyncio.wait_for(refresh, 5)

    assert _observers(index) == {}
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.needs_refresh(config)


@pytest.mark.asyncio
async def test_join_seen_while_a_room_moves_to_a_new_observer_does_not_restore_the_old_observers_row(
    tmp_path: Path,
) -> None:
    """A join the new observer's read lacks must not fall back to the row of an observer that left the room."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_B,)}, hold=asyncio.Event())
    index = AgentReplyMembershipIndex()
    await _refresh(index, config, runtime_paths, a, b)
    assert _observers(index) == {_ROOM: _A}
    # A left the room, and its sync has not delivered its own leave yet; Alice was kicked after that.
    del a.answers[_ROOM]
    index.apply_member_event(config, _OTHER, _member_event(_BOB, "join"), control_user_id=_B)
    assert index.needs_refresh(config)
    refresh = asyncio.create_task(_refresh(index, config, runtime_paths, a, b))
    await _reached(b.reading)

    index.apply_member_event(config, _ROOM, _member_event(_ALICE, "join"), control_user_id=_B)
    assert b.hold is not None
    b.hold.set()
    await asyncio.wait_for(refresh, 5)

    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert _observers(index) == {}
    assert index.needs_refresh(config)


@pytest.mark.asyncio
async def test_join_seen_during_refresh_that_read_a_kick_does_not_grant(tmp_path: Path) -> None:
    """A join either account's sync delivers while the observer's read already lacks that person must not grant."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B)}, hold=asyncio.Event())
    b = _Account(_B, {_ROOM: (_A, _B)})
    index = AgentReplyMembershipIndex()
    refresh = asyncio.create_task(_refresh(index, config, runtime_paths, a, b))
    await _reached(a.reading)

    # Alice joined and was kicked before the read; both syncs still trail the kick.
    for account in (_B, _A):
        index.apply_member_event(config, _ROOM, _member_event(_ALICE, "join"), control_user_id=account)
    assert a.hold is not None
    a.hold.set()
    await asyncio.wait_for(refresh, 5)

    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert _observers(index) == {}
    assert index.needs_refresh(config)


@pytest.mark.asyncio
async def test_grant_room_no_account_joined_stays_unready(tmp_path: Path) -> None:
    """A configured grant room no observing account has joined denies and is reported with its reason."""
    config, runtime_paths = _config(tmp_path, grant_room=True)
    index = AgentReplyMembershipIndex()

    with capture_logs() as logs:
        await _refresh(index, config, runtime_paths, _Account(_A, {_OTHER: (_A, _ALICE)}))

    assert not index.is_allowed(_ALICE, ["project"], config, runtime_paths)
    assert index.grants_pending(config, joined_rooms=["project"], current_room_id=None)
    assert index.needs_refresh(config)
    assert [
        (log["log_level"], log["room_key"], log["reason"])
        for log in logs
        if log["event"] == "agent_reply_grant_room_unready"
    ] == [("warning", "project", "no_observer_joined")]


@pytest.mark.asyncio
async def test_two_config_keys_for_one_room_share_one_observer_and_one_read(tmp_path: Path) -> None:
    """Two grant keys that name the same room get one observer pick and one members read, and both grant."""
    config, runtime_paths = _config(tmp_path, second_key=True)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    picks: list[str] = []

    def pick_observer(room_id: str, user_ids: Sequence[str]) -> str | None:
        """Rank the accounts differently at each call, as cached power that changes between two picks would."""
        picks.append(room_id)
        return user_ids[(len(picks) - 1) % len(user_ids)]

    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, lambda: (_clients(a, b), pick_observer, True))

    assert picks == [_ROOM]
    assert (a.reads, b.reads) == ([_ROOM], [])
    assert {(room.room_key, room.observer_user_id) for room in index.snapshot.rooms} == {
        ("project", _A),
        ("mirror", _A),
    }
    assert index.is_allowed(_ALICE, ["project"], config, runtime_paths)
    assert index.is_allowed(_ALICE, ["mirror"], config, runtime_paths)
    assert not index.needs_refresh(config)


@pytest.mark.asyncio
async def test_power_change_keeps_ready_observer(tmp_path: Path) -> None:
    """A joined, syncing account stays the room's witness when another account gains more power."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()
    await _refresh(index, config, runtime_paths, a, b, power={_A: 100})

    await _refresh(index, config, runtime_paths, a, b, power={_B: 100})

    assert _observers(index) == {_ROOM: _A}
    assert b.reads == []


@pytest.mark.asyncio
async def test_bot_before_first_sync_is_not_picked(tmp_path: Path) -> None:
    """A bot whose receive loop is not open observes no room despite its power."""
    config, runtime_paths = _config(tmp_path)
    room = nio.MatrixRoom(_ROOM, _B)
    room.power_levels.users[_B] = 100
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)}, rooms={_ROOM: room})
    bots = {
        "a": SimpleNamespace(client=a, running=True, observes_room_members=True),
        "b": SimpleNamespace(client=b, running=True, observes_room_members=False),
    }
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, lambda: membership_observers(cast("Mapping[str, AgentBot]", bots)))

    assert _observers(index) == {_ROOM: _A}
    assert b.reads == []

    bots["b"] = SimpleNamespace(client=b, running=True, observes_room_members=True)
    assert membership_observers(cast("Mapping[str, AgentBot]", bots))[1](_ROOM, [_A, _B]) == _B


@pytest.mark.asyncio
async def test_one_failed_joined_rooms_keeps_discovery_unknown(tmp_path: Path) -> None:
    """One account's failed room list leaves the joined rooms unknown, so its rooms wait while the others grant."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _ALICE)}, joined_rooms_fails=True)
    b = _Account(_B, {_OTHER: (_B, _ALICE)})
    index = AgentReplyMembershipIndex()

    await _refresh(index, config, runtime_paths, a, b)

    assert index.is_current_room_member(_ALICE, _OTHER, config, runtime_paths)
    assert not index.knows_joined_rooms()
    assert index.needs_refresh(config)
    assert index.grants_pending(config, joined_rooms=[], current_room_id=_ROOM)

    # A live change in a room another account observes must not drop the retry the failed room list still needs.
    assert index.apply_member_event(config, _OTHER, _member_event(_BOB, "join"), control_user_id=_B)

    assert index.is_current_room_member(_BOB, _OTHER, config, runtime_paths)
    assert index.needs_refresh(config)

    a.joined_rooms_fails = False
    await _refresh(index, config, runtime_paths, a, b)

    assert index.knows_joined_rooms()
    assert not index.needs_refresh(config)
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.is_current_room_member(_BOB, _OTHER, config, runtime_paths)


@pytest.mark.asyncio
async def test_invalidations_during_every_held_read_get_one_retry_then_the_backoff(tmp_path: Path) -> None:
    """An account whose sync keeps losing history voids each attempt; the coordinator retries once, then waits."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _ALICE)})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    outcomes: list[object] = []

    async def refresh() -> object:
        a.hold, a.reading = asyncio.Event(), asyncio.Event()
        attempt = asyncio.create_task(index.refresh(config, runtime_paths, lambda: (_clients(a), _by_power({}), True)))
        await _reached(a.reading)
        membership_sync.invalidate_observer(config, _B, reason="uncertain_sync_response")
        a.hold.set()
        outcomes.append(await asyncio.wait_for(attempt, 5))
        return outcomes[-1]

    index.invalidate(config, reason="initial_config")
    await asyncio.wait_for(membership_sync.refresh_for_access_check(config, refresh), 5)

    assert outcomes == ["voided", "voided"]
    assert membership_sync._refresh_retry_at > 0.0
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    membership_sync.request_refresh(config, refresh)
    await asyncio.sleep(0)
    assert outcomes == ["voided", "voided"]

    a.hold = None
    assert await index.refresh(config, runtime_paths, lambda: (_clients(a), _by_power({}), True)) == "published"
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    a.joined_rooms_fails = True
    index.forget_joined_rooms()
    assert await index.refresh(config, runtime_paths, lambda: (_clients(a), _by_power({}), True)) == "read_failed"


def _own_join(previous_membership: str | None) -> IngestionRecordAdmission:
    """Return an account's own join of ``_ROOM`` as its journal admits it: a lifecycle record, no timeline event."""
    return _ingestion_admission(
        IngestionRecordDisposition.ROOM_LIFECYCLE,
        source=DepartureSource.REPORTED,
        room_id=_ROOM,
        previous_membership=previous_membership,
        membership="join",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_membership", [None, "invite", "leave"])
async def test_own_join_of_a_room_no_room_list_named_waits_for_that_accounts_read(
    tmp_path: Path,
    previous_membership: str | None,
) -> None:
    """A new DM: the agent's own join, with no live member event, makes the first message wait for the room's read."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    await _refresh(index, config, runtime_paths, a)
    assert index.knows_joined_rooms()
    assert not index.needs_refresh(config)
    assert not index.grants_pending(config, joined_rooms=[], current_room_id=_ROOM)
    epoch = index._epoch

    membership_sync.pre_admit_ingestion(
        config,
        runtime_paths,
        _own_join(previous_membership),
        None,
        observer_user_id=_A,
    )

    # A refresh in flight may hold a room list that predates the join too, so this join voids it.
    assert index._epoch == epoch + 1
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.grants_pending(config, joined_rooms=[], current_room_id=_ROOM)
    assert index.needs_refresh(config)

    a.answers[_ROOM] = (_A, _ALICE)
    await _refresh(index, config, runtime_paths, a)

    assert _observers(index) == {_ROOM: _A}
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert not index.needs_refresh(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("published", ["row_of_another_account", "discovery_unknown"])
async def test_own_join_during_the_prospective_observers_read_is_discovery_evidence_only(
    tmp_path: Path,
    published: str,
) -> None:
    """An own join never fences the read that makes its account the observer, never grants, and sets no backoff.

    Either the room's row still names an account that stopped observing, or nothing is known yet, as at a first
    start, when every joined room reports a join: the join then publishes nothing and voids nothing.
    """
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _B, _ALICE)})
    b = _Account(_B, {_ROOM: (_A, _B, _ALICE)})
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    if published == "row_of_another_account":
        await _refresh(index, config, runtime_paths, a, b, power={_B: 50})
        membership_sync.invalidate_observer(config, _B, reason="observer_stopping")
    else:
        index.invalidate(config, reason="initial_config")
    a.hold = asyncio.Event()

    async def refresh() -> object:
        return await index.refresh(config, runtime_paths, lambda: (_clients(a), _by_power({}), True))

    membership_sync.request_refresh(config, refresh)
    await _reached(a.reading)
    epoch, snapshot = index._epoch, index.snapshot

    membership_sync.pre_admit_ingestion(config, runtime_paths, _own_join("invite"), None, observer_user_id=_A)

    assert (index._epoch, index.snapshot) == (epoch, snapshot)
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert index.grants_pending(config, joined_rooms=[], current_room_id=_ROOM)
    a.hold.set()
    await asyncio.wait_for(membership_sync.refresh_for_access_check(config, refresh), 5)

    assert _observers(index) == {_ROOM: _A}
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert a.reads == [_ROOM]
    assert membership_sync._refresh_retry_at == 0.0
    assert not index.needs_refresh(config)


@pytest.mark.asyncio
async def test_own_join_of_a_room_another_account_observes_changes_nothing(tmp_path: Path) -> None:
    """A second account joining a room that already has a ready row neither revokes its grants nor asks for a read."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    await _refresh(index, config, runtime_paths, _Account(_A, {_ROOM: (_A, _ALICE)}), _Account(_B, {}))

    membership_sync.pre_admit_ingestion(config, runtime_paths, _own_join("invite"), None, observer_user_id=_B)

    assert _observers(index) == {_ROOM: _A}
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert not index.needs_refresh(config)


def _bot(account: _Account, *, running: bool = True, observing: bool = True) -> SimpleNamespace:
    return SimpleNamespace(client=account, running=running, observes_room_members=observing)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("b_running", "b_observing", "a_list_fails", "during_refresh", "known"),
    [
        (True, True, False, None, True),
        (False, False, False, None, True),
        (True, False, False, None, False),
        (True, True, True, None, False),
        (False, False, False, "b_starts", False),
        (True, False, False, "b_catches_up", False),
        (True, False, False, "b_stops_running", True),
    ],
)
async def test_census_proves_absence_only_when_every_running_bot_observes_and_answers(
    tmp_path: Path,
    *,
    b_running: bool,
    b_observing: bool,
    a_list_fails: bool,
    during_refresh: str | None,
    known: bool,
) -> None:
    """A room list names every joined room only when each running bot observes, from the start to the publication."""
    config, runtime_paths = _config(tmp_path)
    a = _Account(_A, {_ROOM: (_A, _ALICE)}, joined_rooms_fails=a_list_fails, hold=asyncio.Event())
    b = _Account(_B, {_OTHER: (_B, _ALICE)})
    bots = {"a": _bot(a), "b": _bot(b, running=b_running, observing=b_observing)}
    index = AgentReplyMembershipIndex()

    refresh = asyncio.create_task(
        index.refresh(config, runtime_paths, lambda: membership_observers(cast("Mapping[str, AgentBot]", bots))),
    )
    if not a_list_fails:
        await _reached(a.reading)
    if during_refresh == "b_starts":
        bots["b"].running = True
    elif during_refresh == "b_catches_up":
        bots["b"].observes_room_members = True
    elif during_refresh == "b_stops_running":
        bots["b"].running = False
    assert a.hold is not None
    a.hold.set()
    await asyncio.wait_for(refresh, 5)

    assert index.knows_joined_rooms() is known
    assert index.needs_refresh(config) is not known
    # Unknown means a source in a room no row covers waits; only a complete census settles it as absent.
    assert index.grants_pending(config, joined_rooms=[], current_room_id="!unlisted:example.com") is not known
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths) is not a_list_fails


@pytest.mark.asyncio
async def test_dm_of_an_observer_whose_loop_failed_waits_until_that_observer_caught_up(tmp_path: Path) -> None:
    """A census of the other accounts never proves absent a room only a restarting account has joined."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()
    membership_sync = _sync(index)
    accounts = {_A: _Account(_A, {_ROOM: (_A, _ALICE)}), _B: _Account(_B, {_OTHER: (_B, _ALICE)})}
    bots = {
        user_id: SimpleNamespace(client=account, running=True, observes_room_members=False)
        for user_id, account in accounts.items()
    }

    def pending() -> bool:
        return index.grants_pending(config, joined_rooms=[], current_room_id=_ROOM)

    async def loop_caught_up_then_refresh(*user_ids: str) -> None:
        for user_id in user_ids:
            membership_sync.observer_loop_started(config, user_id)
            membership_sync.observer_frame_settled(user_id)
        for user_id, bot in bots.items():
            bot.observes_room_members = membership_sync.observer_is_open(user_id)
        await index.refresh(config, runtime_paths, lambda: membership_observers(cast("Mapping[str, AgentBot]", bots)))

    await loop_caught_up_then_refresh(_A, _B)
    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)

    membership_sync.observer_closed(config, _A, reason="sync_failure")
    assert (index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths), pending()) == (False, True)

    await loop_caught_up_then_refresh()
    assert accounts[_A].reads == [_ROOM]
    assert (index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths), pending()) == (False, True)
    assert index.is_current_room_member(_ALICE, _OTHER, config, runtime_paths)

    await loop_caught_up_then_refresh(_A)
    assert (index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths), pending()) == (True, False)
    assert index.knows_joined_rooms()


@pytest.mark.asyncio
async def test_without_any_observer_every_room_waits(tmp_path: Path) -> None:
    """Before any bot's first sync nothing is known, so a source waits for a refresh instead of being denied."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()

    await _refresh(index, config, runtime_paths)

    assert not index.knows_joined_rooms()
    assert index.needs_refresh(config)
    assert index.grants_pending(config, joined_rooms=[], current_room_id=_ROOM)


@pytest.mark.asyncio
async def test_two_bots_report_same_event_applies_once(tmp_path: Path) -> None:
    """Every bot reports each membership event it syncs; only the observer's copy changes grants and runs effects."""
    config, runtime_paths = _config(tmp_path)
    index = AgentReplyMembershipIndex()
    bots, calls = _fleet("a", "b")
    membership_sync = AgentReplyMembershipSync(index, lambda: bots)
    await _refresh(index, config, runtime_paths, _Account(_A, {_ROOM: (_A, _B)}), _Account(_B, {_ROOM: (_A, _B)}))
    join = _member_event(_ALICE, "join")

    await membership_sync.apply_live_transition(config, _ROOM, join, control_user_id=_B)

    assert calls.log == []
    assert not index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)

    await membership_sync.apply_live_transition(config, _ROOM, join, control_user_id=_A)

    assert index.is_current_room_member(_ALICE, _ROOM, config, runtime_paths)
    assert calls.ops("reconcile") == ["a", "b"]

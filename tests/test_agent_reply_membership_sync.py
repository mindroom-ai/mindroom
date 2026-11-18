"""Tests for reply-membership sync coordination and what each grant change asks of every bot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.event_journal import (
    DepartureSource,
    EventClass,
    EventKind,
    InboundEvent,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
)
from tests.conftest import test_runtime_paths
from tests.test_reply_grant_call_effects import _Calls

if TYPE_CHECKING:
    from pathlib import Path

_JOIN = nio.RoomMemberEvent.from_dict(
    {
        "type": "m.room.member",
        "event_id": "$grant",
        "sender": "@alice:localhost",
        "state_key": "@alice:localhost",
        "origin_server_ts": 1,
        "content": {"membership": "join"},
    },
)
assert isinstance(_JOIN, nio.RoomMemberEvent)
_OBSERVER = "@mindroom_observer:localhost"


@dataclass
class _Bot:
    """A fleet member that records in ``calls`` what grant changes ask of it, optionally failing its revocation."""

    name: str
    calls: _Calls
    failures: list[BaseException] = field(default_factory=list)

    def schedule_pending_invite_reconciliation(self) -> None:
        self.calls.log.append((self.name, "invites"))

    async def revoke_reply_authorized_calls(self) -> None:
        if self.failures:
            raise self.failures.pop(0)
        await self.calls.revoke(self.name)

    def schedule_reply_authorized_call_reconciliation(self) -> None:
        self.calls.log.append((self.name, "reconcile"))

    def schedule_reply_authorized_call_revocation(self) -> None:
        self.calls.log.append((self.name, "scheduled_revoke"))


def _ingestion_admission(
    disposition: IngestionRecordDisposition,
    *,
    source: DepartureSource | None = None,
    room_id: str | None = None,
    previous_membership: str | None = None,
    membership: str | None = None,
    event: InboundEvent | None = None,
) -> IngestionRecordAdmission:
    return IngestionRecordAdmission(
        disposition=disposition,
        source=source,
        room_id=room_id,
        previous_membership=previous_membership,
        membership=membership,
        previous_membership_epoch=0 if room_id is not None else None,
        membership_epoch=1 if room_id is not None else None,
        event=event,
        projected=None,
    )


def _fleet(*names: str, hold: asyncio.Event | None = None) -> tuple[list[_Bot], _Calls]:
    calls = _Calls(hold=hold)
    return [_Bot(name, calls) for name in names], calls


def test_history_loss_invalidates_grants_and_revokes_every_bots_calls_before_admission(tmp_path: Path) -> None:
    """A history-loss carrier fails its account's rooms closed and schedules every bot's revocation synchronously."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    bots, calls = _fleet("router", "voice")
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)
    config = Config()

    membership_sync.pre_admit_ingestion(
        config,
        test_runtime_paths(tmp_path),
        _ingestion_admission(IngestionRecordDisposition.HISTORY_LOSS, room_id="!uncertain:localhost"),
        None,
        observer_user_id=_OBSERVER,
    )

    memberships.invalidate_observer.assert_called_once_with(config, _OBSERVER, reason="uncertain_sync_response")
    memberships.invalidate.assert_not_called()
    memberships.mark_room_unready.assert_not_called()
    assert calls.log == [("router", "scheduled_revoke"), ("voice", "scheduled_revoke")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_room_members", "members_of_rooms", "expected_refreshes"),
    [(False, [], 0), (True, [], 1), (False, ["lobby"], 1)],
)
async def test_history_loss_refreshes_only_room_backed_grants(
    current_room_members: bool,
    members_of_rooms: list[str],
    expected_refreshes: int,
) -> None:
    """Explicit-user access must not trigger room/call scans after every sync gap."""
    config = Config(
        router=RouterConfig(access=ResponderAccessConfig(current_room_members=False)),
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                role="Assistant",
                rooms=["lobby"],
                access=ResponderAccessConfig(
                    current_room_members=current_room_members,
                    members_of_rooms=members_of_rooms,
                    users=["@alice:localhost"],
                ),
            ),
        },
    )
    memberships = AgentReplyMembershipIndex()
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    membership_sync.invalidate_observer(config, _OBSERVER, reason="uncertain_sync_response")
    await membership_sync.refresh_if_needed(config, refresh)

    assert refreshes == expected_refreshes
    assert memberships.needs_refresh(config) is bool(expected_refreshes)


@pytest.mark.parametrize(
    ("admission", "provenance", "reason"),
    [
        (
            _ingestion_admission(
                IngestionRecordDisposition.ROOM_LIFECYCLE,
                source=DepartureSource.REPORTED,
                room_id="!departed:localhost",
                previous_membership="join",
                membership="leave",
            ),
            None,
            "control_client_departed",
        ),
        (
            _ingestion_admission(
                IngestionRecordDisposition.COMPATIBILITY_ONLY,
                event=InboundEvent(
                    event_id="$member",
                    room_id="!departed:localhost",
                    thread_id=None,
                    kind=EventKind.ROOM_LIFECYCLE,
                    event_class=EventClass.CONTEXT_ONLY,
                    sender="@alice:localhost",
                    origin_server_ts=1,
                    source={},
                ),
            ),
            nio.TimelineEventProvenance.RECOVERED,
            "recovered_membership",
        ),
    ],
)
@pytest.mark.parametrize("grant_changed", [True, False])
def test_room_fence_revokes_every_bots_calls_before_admission(
    tmp_path: Path,
    admission: IngestionRecordAdmission,
    provenance: nio.TimelineEventProvenance | None,
    reason: str,
    *,
    grant_changed: bool,
) -> None:
    """A reported departure or recovered membership fences its room for its account; a changed grant revokes calls."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.mark_room_unready.return_value = grant_changed
    bots, calls = _fleet("router", "voice")
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)
    config = Config()
    runtime_paths = test_runtime_paths(tmp_path)

    membership_sync.pre_admit_ingestion(config, runtime_paths, admission, provenance, observer_user_id=_OBSERVER)

    memberships.mark_room_unready.assert_called_once_with(
        config,
        runtime_paths,
        "!departed:localhost",
        observer_user_id=_OBSERVER,
        reason=reason,
    )
    memberships.invalidate_observer.assert_not_called()
    assert calls.log == ([("router", "scheduled_revoke"), ("voice", "scheduled_revoke")] if grant_changed else [])


def test_carrier_without_membership_does_not_change_reply_memberships(tmp_path: Path) -> None:
    """A carrier without an own-membership change leaves reply authorization and every call unchanged."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    bots, calls = _fleet("router")
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)

    membership_sync.pre_admit_ingestion(
        Config(),
        test_runtime_paths(tmp_path),
        _ingestion_admission(IngestionRecordDisposition.COMPATIBILITY_ONLY),
        None,
        observer_user_id=_OBSERVER,
    )

    memberships.invalidate_observer.assert_not_called()
    memberships.mark_room_unready.assert_not_called()
    assert calls.log == []


def test_own_join_asks_for_the_rooms_read_and_revokes_no_call(tmp_path: Path) -> None:
    """An account's own join marks the room for a read without a backoff; it loses no grant, so no call is revoked."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.mark_room_unready.return_value = True
    bots, calls = _fleet("router")
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)
    membership_sync._refresh_retry_at = float("inf")
    config = Config()
    runtime_paths = test_runtime_paths(tmp_path)

    membership_sync.pre_admit_ingestion(
        config,
        runtime_paths,
        _ingestion_admission(
            IngestionRecordDisposition.ROOM_LIFECYCLE,
            source=DepartureSource.REPORTED,
            room_id="!joined:localhost",
            previous_membership="leave",
            membership="join",
        ),
        None,
        observer_user_id=_OBSERVER,
    )

    memberships.mark_room_unready.assert_called_once_with(
        config,
        runtime_paths,
        "!joined:localhost",
        observer_user_id=_OBSERVER,
        reason="observer_joined",
    )
    assert membership_sync._refresh_retry_at == 0.0
    assert calls.log == []


def test_every_invalidation_revokes_and_none_starts_calls() -> None:
    """Repeated losses of grant authority each revoke, because any of them may follow restored authority."""
    bots, calls = _fleet("router", "voice")
    membership_sync = AgentReplyMembershipSync(AgentReplyMembershipIndex(), lambda: bots)

    membership_sync.invalidate(Config(), reason="config_reload")
    membership_sync.invalidate_observer(Config(), _OBSERVER, reason="sync_loop_started")

    assert calls.ops("scheduled_revoke") == ["router", "router", "voice", "voice"]
    assert calls.ops("reconcile") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refreshed", [True, False])
async def test_grant_change_revokes_every_bots_calls_before_reconciling_any(*, refreshed: bool) -> None:
    """While one bot's revocation is held no bot starts calls; then every bot does, even while a refresh is owed.

    Each call start rechecks its requester against current grants, so a grant room that stays unready denies only the
    calls it alone would allow.
    """
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    memberships.apply_member_event.return_value = True
    hold = asyncio.Event()
    bots, calls = _fleet("router", "voice", hold=hold)
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)

    change = asyncio.create_task(
        membership_sync.apply_grant_change()
        if refreshed
        else membership_sync.apply_live_transition(Config(), "!grant:localhost", _JOIN, control_user_id="@r:localhost"),
    )
    await calls.wait_for_revocations(2)
    assert calls.ops("invites") == ["router", "voice"]
    assert calls.ops("reconcile") == []
    hold.set()
    await change

    assert calls.ops("revoked") == ["router", "voice"]
    assert calls.ops("reconcile") == ["router", "voice"]
    assert calls.first("reconcile") > calls.last("revoked")


@pytest.mark.asyncio
async def test_bot_added_during_revocation_is_left_to_its_own_first_frame() -> None:
    """One grant change asks one cohort; a bot started during its revocation reconciles calls at its own start."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    hold = asyncio.Event()
    bots, calls = _fleet("router", hold=hold)
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)

    change = asyncio.create_task(membership_sync.apply_grant_change())
    await calls.wait_for_revocations(1)
    bots.append(_Bot("voice", calls))
    hold.set()
    await change

    assert calls.ops("reconcile") == ["router"]
    assert calls.ops("invites") == ["router"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["failure", "cancellation"])
async def test_live_transition_retries_unfinished_effects_after_event_replay(interruption: str) -> None:
    """A failed or cancelled revocation starts no call, and a no-op replay retries every effect until they finish."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.apply_member_event.side_effect = [True, False, False]
    hold = asyncio.Event() if interruption == "cancellation" else None
    bots, calls = _fleet("router", "voice", hold=hold)
    if interruption == "failure":
        bots[1].failures.append(RuntimeError("revocation failed"))
    membership_sync = AgentReplyMembershipSync(memberships, lambda: bots)

    def apply_transition() -> asyncio.Task[None]:
        return asyncio.create_task(
            membership_sync.apply_live_transition(Config(), "!grant:localhost", _JOIN, control_user_id="@r:localhost"),
        )

    first = apply_transition()
    if interruption == "failure":
        with pytest.raises(RuntimeError, match="revocation failed"):
            await first
    else:
        await calls.wait_for_revocations(2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert hold is not None
        hold.set()
    assert calls.ops("reconcile") == []

    await apply_transition()
    await apply_transition()

    assert calls.ops("invites") == ["router", "router", "voice", "voice"]
    assert calls.ops("reconcile") == ["router", "voice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["invalidation", "observer_invalidation", "room_fence"])
async def test_new_loss_of_grant_authority_refreshes_without_an_earlier_backoff(tmp_path: Path, loss: str) -> None:
    """A failed refresh waits out its backoff, but a new loss of authority refreshes at once."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    memberships.mark_room_unready.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    refreshes = 0

    async def failed_refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    membership_sync.invalidate(config, reason="uncertain_sync_response")
    await membership_sync.refresh_if_needed(config, failed_refresh)
    await membership_sync.refresh_if_needed(config, failed_refresh)
    assert refreshes == 1
    if loss == "invalidation":
        membership_sync.invalidate(config, reason="uncertain_sync_response")
    elif loss == "observer_invalidation":
        membership_sync.invalidate_observer(config, _OBSERVER, reason="uncertain_sync_response")
    else:
        membership_sync.pre_admit_ingestion(
            config,
            test_runtime_paths(tmp_path),
            _ingestion_admission(
                IngestionRecordDisposition.ROOM_LIFECYCLE,
                source=DepartureSource.REPORTED,
                room_id="!grant:localhost",
                previous_membership="join",
                membership="leave",
            ),
            None,
            observer_user_id=_OBSERVER,
        )
    await membership_sync.refresh_if_needed(config, failed_refresh)

    assert refreshes == 2


@pytest.mark.asyncio
async def test_sync_frames_request_one_refresh_without_waiting_for_it() -> None:
    """A bot's sync frame only asks for the refresh, so a second bot's frame goes on while the first one's is open."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    answered = asyncio.Event()
    refreshes = 0

    async def held_refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        await answered.wait()
        memberships.needs_refresh.return_value = False

    assert membership_sync.request_refresh(config, held_refresh) is None
    await asyncio.sleep(0)
    assert membership_sync.request_refresh(config, held_refresh) is None
    await asyncio.sleep(0)

    assert refreshes == 1
    answered.set()
    await membership_sync.refresh_for_access_check(config, held_refresh)
    assert refreshes == 1
    assert not memberships.needs_refresh(config)


@pytest.mark.asyncio
async def test_refresh_voided_by_a_grant_loss_runs_again_at_once_without_a_failure_backoff() -> None:
    """Only a failed read backs off: a refresh a grant loss voided is followed by another in the same task."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        if refreshes == 1:
            # A bot's receive loop failed while this refresh read Matrix, which voids it.
            membership_sync.invalidate_observer(config, _OBSERVER, reason="sync_failure")
        elif refreshes == 2:
            memberships.needs_refresh.return_value = False

    membership_sync.request_refresh(config, refresh)
    await asyncio.wait_for(membership_sync.refresh_for_access_check(config, refresh), 5)

    assert refreshes == 2
    assert membership_sync._refresh_retry_at == 0.0

    memberships.needs_refresh.return_value = True
    refreshes = 2
    membership_sync.request_refresh(config, refresh)
    await asyncio.wait_for(membership_sync.refresh_for_access_check(config, refresh), 5)

    assert refreshes == 3
    assert membership_sync._refresh_retry_at > 0.0


@pytest.mark.asyncio
async def test_close_cancels_the_refresh_in_flight_and_waits_for_its_end() -> None:
    """The runtime ends the shared refresh once, at process shutdown."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    started = asyncio.Event()
    ended: list[str] = []

    async def held_refresh() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.append("refresh")

    await membership_sync.close()
    membership_sync.request_refresh(config, held_refresh)
    await asyncio.wait_for(started.wait(), 5)

    await asyncio.wait_for(membership_sync.close(), 5)

    assert ended == ["refresh"]


@pytest.mark.asyncio
async def test_startup_gate_waits_at_most_ten_seconds_for_a_slow_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow refresh keeps running after the startup gate stops waiting, and a later wait joins it instead of another."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    loop = asyncio.get_running_loop()
    now = 0
    monkeypatch.setattr(loop, "time", lambda: now)
    answered = asyncio.Event()
    refreshes = 0

    async def slow_refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        await answered.wait()
        memberships.needs_refresh.return_value = False

    for frame_started in (0, 10):
        frame = asyncio.create_task(membership_sync.refresh_if_needed(config, slow_refresh))
        while not frame.done() and now < 60:
            # Drain ready callbacks and timeout cancellation without wall-clock sleeps.
            for _ in range(20):
                await asyncio.sleep(0)
            if not frame.done():
                now += 1
        assert now == frame_started + 10
    assert refreshes == 1
    answered.set()
    await membership_sync.refresh_if_needed(config, slow_refresh)
    assert refreshes == 1
    assert not memberships.needs_refresh(config)


@pytest.mark.asyncio
async def test_a_stopping_observer_does_not_cancel_the_refresh() -> None:
    """No bot owns the refresh: one that stops only invalidates its rooms, and the refresh runs to its end."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def slow_refresh() -> None:
        nonlocal finished
        started.set()
        await release.wait()
        finished = True

    frame = asyncio.create_task(membership_sync.refresh_if_needed(config, slow_refresh))
    await started.wait()
    membership_sync.invalidate_observer(config, _OBSERVER, reason="observer_stopping")
    release.set()
    await frame

    assert finished
    memberships.invalidate_observer.assert_called_once_with(config, _OBSERVER, reason="observer_stopping")


@pytest.mark.asyncio
async def test_a_sync_frame_never_waits_on_a_refresh_it_does_not_need() -> None:
    """A refresh an access check started to read one room again is no reason for a bot's frame to wait."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    started = asyncio.Event()
    release = asyncio.Event()
    refreshes = 0

    async def held_refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        # The refresh took the room to read again, so grants need nothing more while it queries Matrix.
        memberships.needs_refresh.return_value = False
        started.set()
        await release.wait()

    check = asyncio.create_task(membership_sync.refresh_for_access_check(config, held_refresh))
    await started.wait()
    async with asyncio.timeout(1):
        await membership_sync.refresh_if_needed(config, held_refresh)

    assert not check.done()
    release.set()
    await check
    assert refreshes == 1


@pytest.mark.asyncio
async def test_backoff_stays_bounded_however_many_refreshes_fail() -> None:
    """Access checks may retry a grant room that never resolves far more often than the router's frames do."""
    config = Config()
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships, lambda: ())

    async def unresolved() -> None:
        pass

    with capture_logs() as logs:
        for _ in range(1100):
            await membership_sync.refresh_for_access_check(config, unresolved)

    assert [log for log in logs if log["log_level"] == "error"] == []

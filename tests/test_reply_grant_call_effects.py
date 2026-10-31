"""A reply-grant change reaches every bot's calls in one order: revoke first, then reconcile what became allowed."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nio import TimelineEventProvenance

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.calls import CallsConfig, RealtimeCallProfile
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.invited_rooms_store import invited_rooms_path, remember_invited_room
from mindroom.matrix.state import MatrixState
from mindroom.matrix_rtc.call_manager import CallManager
from tests.conftest import start_call_manager
from tests.matrix_state_helpers import save_matrix_state
from tests.test_bot_ready_hook import (
    _FRESH_SEMANTIC_FACTS,
    _REPLAY_FACTS,
    _agent_bot,
    _complete_frame,
    _departure_member_event,
    _grant_room_client,
    _validated_reported_membership_admission,
    _validated_timeline_admission,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from tests.runtime_fakes import ReadyRuntime

GRANT_ROOM_ID = "!grant:localhost"
JOINER = "@bob:localhost"


@dataclass
class _Calls:
    """What every bot's call manager was asked, in order, with a switch that holds revocations in progress."""

    log: list[tuple[str, str]] = field(default_factory=list)
    hold: asyncio.Event | None = None
    revocations_started: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(0))

    def manager(self, agent_name: str) -> MagicMock:
        manager = MagicMock(spec=CallManager)
        manager.active_call_requesters = ()

        async def revoke() -> None:
            self.log.append((agent_name, "revoke"))
            self.revocations_started.release()
            if self.hold is not None:
                await self.hold.wait()
            self.log.append((agent_name, "revoked"))

        async def reconcile() -> None:
            self.log.append((agent_name, "reconcile"))

        manager.revoke_reply_authorization.side_effect = revoke
        manager.reconcile_reply_authorization.side_effect = reconcile
        return manager

    def ops(self, op: str) -> list[str]:
        return sorted(agent_name for agent_name, logged in self.log if logged == op)

    def first(self, op: str) -> int:
        return min(index for index, (_agent_name, logged) in enumerate(self.log) if logged == op)

    def last(self, op: str) -> int:
        return max(index for index, (_agent_name, logged) in enumerate(self.log) if logged == op)

    async def wait_for_revocations(self, count: int) -> None:
        for _ in range(count):
            await asyncio.wait_for(self.revocations_started.acquire(), timeout=1)


async def _router_and_agent(
    tmp_path: Path,
    calls: _Calls,
    *,
    grant_rooms: tuple[str, ...] = ("grant",),
) -> tuple[AgentBot, dict[str, MagicMock]]:
    """Return a router beside an agent, each with a logging call manager, the router joined to the grant room."""
    router = _agent_bot(tmp_path, agent_name=ROUTER_AGENT_NAME)
    agent = _agent_bot(tmp_path, agent_name="code", orchestrator=cast("ReadyRuntime", router.orchestrator))
    router.config.router.access = ResponderAccessConfig(members_of_rooms=list(grant_rooms))
    router.client = _grant_room_client(router, GRANT_ROOM_ID)
    managers = {bot.agent_name: calls.manager(bot.agent_name) for bot in (router, agent)}
    for bot in (router, agent):
        await start_call_manager(bot, managers[bot.agent_name])
    return router, managers


async def _settle() -> None:
    assert await wait_for_background_tasks(timeout=1)


def _live_join(router: AgentBot, event_id: str = "$join", ts: int = 1) -> object:
    return _validated_timeline_admission(
        router,
        GRANT_ROOM_ID,
        _departure_member_event(event_id, user_id=JOINER, membership="join", ts=ts),
    )


@pytest.mark.asyncio
async def test_authoritative_refresh_revokes_every_bots_calls_before_reconciling_any(tmp_path: Path) -> None:
    """No call starts for a newly allowed requester until every bot finished ending denied calls."""
    hold = asyncio.Event()
    calls = _Calls(hold=hold)
    router, _managers = await _router_and_agent(tmp_path, calls)

    refresh = asyncio.create_task(router.orchestrator.refresh_agent_reply_memberships())
    try:
        await calls.wait_for_revocations(2)
        assert calls.ops("reconcile") == []
    finally:
        hold.set()
        await refresh
    await _settle()

    assert calls.ops("reconcile") == ["code", "router"]
    assert calls.first("reconcile") > calls.last("revoked")


@pytest.mark.asyncio
async def test_authoritative_refresh_invalidated_during_revocation_reconciles_nothing(tmp_path: Path) -> None:
    """A gap that opens while a refresh revokes owes a fresh refresh, so nothing starts and every bot revokes again."""
    hold = asyncio.Event()
    calls = _Calls(hold=hold)
    router, _managers = await _router_and_agent(tmp_path, calls)

    refresh = asyncio.create_task(router.orchestrator.refresh_agent_reply_memberships())
    try:
        await calls.wait_for_revocations(2)
        router.invalidate_agent_reply_memberships(reason="uncertain_sync_response")
    finally:
        hold.set()
        await refresh
    await _settle()

    assert calls.ops("revoke") == ["code", "code", "router", "router"]
    assert calls.ops("reconcile") == []


@pytest.mark.asyncio
async def test_every_loss_of_grant_authority_revokes_every_bots_calls(tmp_path: Path) -> None:
    """Each invalidation revokes every bot's calls and starts none; only a complete refresh starts allowed calls."""
    calls = _Calls()
    router, _managers = await _router_and_agent(tmp_path, calls)

    router.invalidate_agent_reply_memberships(reason="uncertain_sync_response")
    router.invalidate_agent_reply_memberships(reason="sync_loop_started")
    await _settle()
    assert calls.ops("revoke") == ["code", "code", "router", "router"]
    assert calls.ops("reconcile") == []

    await router.orchestrator.refresh_agent_reply_memberships()
    await _settle()
    assert calls.ops("revoke") == ["code", "code", "code", "router", "router", "router"]
    assert calls.ops("reconcile") == ["code", "router"]

    router.invalidate_agent_reply_memberships(reason="uncertain_sync_response")
    router.invalidate_agent_reply_memberships(reason="uncertain_sync_response")
    await _settle()
    assert calls.ops("revoke") == ["code"] * 5 + ["router"] * 5
    assert calls.ops("reconcile") == ["code", "router"]


@pytest.mark.asyncio
async def test_live_grant_change_revokes_every_bots_calls_before_reconciling_any(tmp_path: Path) -> None:
    """A live roster change in a grant room ends denied calls on every bot before any newly allowed call starts."""
    hold = asyncio.Event()
    calls = _Calls(hold=hold)
    router, _managers = await _router_and_agent(tmp_path, calls)
    index = router._runtime_view.agent_reply_memberships
    await index.refresh(router.config, router.runtime_paths, router.client)

    effects = asyncio.create_task(
        router._after_ingestion_admission(_live_join(router), _FRESH_SEMANTIC_FACTS, TimelineEventProvenance.LIVE),
    )
    try:
        await calls.wait_for_revocations(2)
        assert calls.ops("reconcile") == []
    finally:
        hold.set()
        await effects
    await _settle()

    assert index.is_allowed(JOINER, ["grant"], router.config, router.runtime_paths)
    assert calls.ops("reconcile") == ["code", "router"]
    assert calls.first("reconcile") > calls.last("revoked")


@pytest.mark.asyncio
async def test_live_grant_change_reconciles_calls_while_another_grant_room_awaits_refresh(tmp_path: Path) -> None:
    """A live change in a ready grant room reconciles calls at once, even while another grant room needs a refresh."""
    calls = _Calls()
    router, _managers = await _router_and_agent(tmp_path, calls, grant_rooms=("grant", "other"))
    state = MatrixState.load(runtime_paths=router.runtime_paths)
    state.add_room("other", "!other:localhost", "#other:localhost", "Other")
    save_matrix_state(state, router.runtime_paths)
    index = router._runtime_view.agent_reply_memberships
    await index.refresh(router.config, router.runtime_paths, router.client)
    assert index.needs_refresh(router.config)

    await router._after_ingestion_admission(_live_join(router), _FRESH_SEMANTIC_FACTS, TimelineEventProvenance.LIVE)
    await _settle()

    assert index.is_allowed(JOINER, ["grant"], router.config, router.runtime_paths)
    assert calls.ops("revoke") == ["code", "router"]
    assert calls.ops("reconcile") == ["code", "router"]


@pytest.mark.asyncio
async def test_live_grant_change_retries_every_bots_effects_after_a_failed_revocation(tmp_path: Path) -> None:
    """A revocation that fails reconciles nothing, and the replayed transition revokes and reconciles again."""
    calls = _Calls()
    router, managers = await _router_and_agent(tmp_path, calls)
    index = router._runtime_view.agent_reply_memberships
    await index.refresh(router.config, router.runtime_paths, router.client)
    failing = managers["code"].revoke_reply_authorization
    revoke = failing.side_effect

    async def fail_once() -> None:
        if failing.await_count == 1:
            msg = "revocation interrupted"
            raise RuntimeError(msg)
        await revoke()

    failing.side_effect = fail_once
    admission = _live_join(router)

    with pytest.raises(RuntimeError, match="revocation interrupted"):
        await router._after_ingestion_admission(admission, _FRESH_SEMANTIC_FACTS, TimelineEventProvenance.LIVE)
    await _settle()
    assert calls.ops("reconcile") == []

    await router._after_ingestion_admission(admission, _REPLAY_FACTS, TimelineEventProvenance.LIVE)
    await _settle()

    assert calls.ops("revoked") == ["code", "router", "router"]
    assert calls.ops("reconcile") == ["code", "router"]


@pytest.mark.asyncio
async def test_router_departure_schedules_every_bots_revocation_before_admission(tmp_path: Path) -> None:
    """A reported router departure from a grant room schedules revocation before the departure is admitted."""
    calls = _Calls()
    router, _managers = await _router_and_agent(tmp_path, calls)
    index = router._runtime_view.agent_reply_memberships
    await index.refresh(router.config, router.runtime_paths, router.client)
    departure = _validated_reported_membership_admission(
        router,
        GRANT_ROOM_ID,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )

    before = asyncio.all_tasks()
    router._before_ingestion_admission(departure, TimelineEventProvenance.LIVE)
    scheduled = asyncio.all_tasks() - before

    assert scheduled
    assert calls.log == []
    await asyncio.gather(*scheduled)
    await _settle()
    assert calls.ops("revoked") == ["code", "router"]
    assert calls.ops("reconcile") == []


@pytest.mark.asyncio
async def test_call_reconciliation_waits_for_a_frame_published_while_accepting_work(tmp_path: Path) -> None:
    """A room's call reconciliation survives frames published while draining and runs at the next accepting frame."""
    bot = _agent_bot(tmp_path)
    manager = MagicMock(spec=CallManager)
    await start_call_manager(bot, manager)

    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        bot._runtime_view.begin_draining()
        await bot._post_join_room_setup("!call:localhost")
        await _complete_frame(bot)
        await _settle()
        manager.reconcile_joined_rooms.assert_not_awaited()

        assert bot._runtime_view.resume()
        await _complete_frame(bot)
        await _settle()
        await _complete_frame(bot)
        await _settle()

    manager.reconcile_joined_rooms.assert_awaited_once_with(frozenset({"!call:localhost"}))


@pytest.mark.asyncio
async def test_full_call_reconciliation_covers_room_requests(tmp_path: Path) -> None:
    """A full reconciliation requested beside room-scoped ones runs once over every joined room."""
    bot = _agent_bot(tmp_path)
    manager = MagicMock(spec=CallManager)
    await start_call_manager(bot, manager)

    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await bot._post_join_room_setup("!first:localhost")
        bot.mark_sync_loop_started()
        await bot._post_join_room_setup("!second:localhost")
        await _complete_frame(bot)
        await _settle()

    manager.reconcile_joined_rooms.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_call_manager_reads_invited_rooms_of_the_published_config(tmp_path: Path) -> None:
    """A reload that adds a call agent, and a later accepted invite, reach the running manager without a rebuild."""
    bot = _agent_bot(tmp_path)
    build = await start_call_manager(bot, MagicMock(spec=CallManager))
    invited_call_rooms = build.await_args.kwargs["get_invited_rooms_by_agent"]
    assert invited_call_rooms() == {}

    config = bot.config.model_copy(deep=True)
    config.calls = CallsConfig(
        enabled=True,
        profiles={
            "voice": RealtimeCallProfile(
                backend="realtime",
                model="gpt-realtime",
                credentials_service="openai",
                voice="marin",
            ),
        },
        agents={"code": "voice"},
    )
    bot.orchestrator.config = config
    assert invited_call_rooms() == {"code": frozenset()}

    remember_invited_room(invited_rooms_path(bot.runtime_paths, "code"), "!invited:localhost")

    assert invited_call_rooms() == {"code": frozenset({"!invited:localhost"})}


@pytest.fixture
def _short_stop_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound each bot's stop drain so work it cancels is cancelled within the test."""
    monkeypatch.setattr("mindroom.bot.SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.3)


@dataclass
class _HeldReconciliation:
    """The agent's positive call reconciliation, held once started, recording how it ended."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.finished.set()


async def _router_after_a_live_grant_change_with_held_agent_call_start(
    tmp_path: Path,
) -> tuple[AgentBot, AgentBot, dict[str, MagicMock], _HeldReconciliation]:
    """Return a router and agent after a live grant change, the agent's positive call reconciliation held."""
    calls = _Calls()
    router, managers = await _router_and_agent(tmp_path, calls)
    agent = router.orchestrator.agent_bots["code"]
    await router._runtime_view.agent_reply_memberships.refresh(router.config, router.runtime_paths, router.client)
    held = _HeldReconciliation()
    managers["code"].reconcile_reply_authorization.side_effect = held.run
    await router._after_ingestion_admission(_live_join(router), _FRESH_SEMANTIC_FACTS, TimelineEventProvenance.LIVE)
    await asyncio.wait_for(held.started.wait(), timeout=1)
    return router, agent, managers, held


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_stop_drain")
async def test_a_bots_stop_drains_the_call_work_a_live_grant_change_gave_it(tmp_path: Path) -> None:
    """The agent's own stop, not the router's, owns call work a live grant change started for that agent."""
    _router, agent, managers, held = await _router_after_a_live_grant_change_with_held_agent_call_start(tmp_path)

    await agent.stop()

    managers["code"].shutdown.assert_awaited_once_with()
    assert held.cancelled.is_set()
    assert await wait_for_background_tasks(timeout=0, owner=agent._runtime_view)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_stop_drain")
async def test_router_stop_leaves_another_bots_call_start_running(tmp_path: Path) -> None:
    """A router stopping after a live grant change neither waits for nor cancels another agent's call start."""
    router, agent, _managers, held = await _router_after_a_live_grant_change_with_held_agent_call_start(tmp_path)

    await router.stop()
    assert not held.cancelled.is_set()
    assert not held.finished.is_set()

    held.release.set()
    assert await wait_for_background_tasks(timeout=1, owner=agent._runtime_view)
    assert held.finished.is_set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_stop_drain")
async def test_router_stop_leaves_another_bots_fence_revocation_running(tmp_path: Path) -> None:
    """A revocation a router fence scheduled for another agent completes even when the router stops first."""
    hold = asyncio.Event()
    calls = _Calls(hold=hold)
    router, _managers = await _router_and_agent(tmp_path, calls)
    agent = router.orchestrator.agent_bots["code"]
    await router._runtime_view.agent_reply_memberships.refresh(router.config, router.runtime_paths, router.client)
    departure = _validated_reported_membership_admission(
        router,
        GRANT_ROOM_ID,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    router._before_ingestion_admission(departure, TimelineEventProvenance.LIVE)
    await calls.wait_for_revocations(2)

    await router.stop()
    hold.set()

    assert await wait_for_background_tasks(timeout=1, owner=agent._runtime_view)
    assert "code" in calls.ops("revoked")


@pytest.mark.asyncio
@pytest.mark.parametrize("draining", [False, True])
async def test_a_draining_bot_starts_no_invite_or_call_work_but_still_revokes(
    tmp_path: Path,
    *,
    draining: bool,
) -> None:
    """Positive work goes only to bots that accept work; revocation reaches a bot whose drain already began."""
    calls = _Calls()
    router, _managers = await _router_and_agent(tmp_path, calls)
    agent = router.orchestrator.agent_bots["code"]
    agent.client = _grant_room_client(agent, GRANT_ROOM_ID)
    await router._runtime_view.agent_reply_memberships.refresh(router.config, router.runtime_paths, router.client)
    if draining:
        agent._runtime_view.begin_draining()

    with patch.object(agent._room_lifecycle, "reconcile_pending_invites", new=AsyncMock()) as invites:
        await router._after_ingestion_admission(
            _live_join(router),
            _FRESH_SEMANTIC_FACTS,
            TimelineEventProvenance.LIVE,
        )
        await _settle()

    assert calls.ops("revoked") == ["code", "router"]
    assert calls.ops("reconcile") == (["router"] if draining else ["code", "router"])
    assert invites.await_count == (0 if draining else 1)


@pytest.mark.asyncio
async def test_call_reconciliation_waits_when_draining_begins_during_frame_effects(tmp_path: Path) -> None:
    """A drain that begins while a frame runs its side effects leaves pending call work for the next accepting frame."""
    bot = _agent_bot(tmp_path)
    manager = MagicMock(spec=CallManager)
    await start_call_manager(bot, manager)
    await bot._post_join_room_setup("!call:localhost")

    async def begin_draining(**_kwargs: object) -> None:
        bot._runtime_view.begin_draining()

    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock(side_effect=begin_draining)):
        await _complete_frame(bot)
    await _settle()
    manager.reconcile_joined_rooms.assert_not_awaited()

    assert bot._runtime_view.resume()
    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await _complete_frame(bot)
    await _settle()
    manager.reconcile_joined_rooms.assert_awaited_once_with(frozenset({"!call:localhost"}))


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_stop_drain")
async def test_stop_drops_pending_call_reconciliation(tmp_path: Path) -> None:
    """A stop forgets requested call work, so a call manager started again does not inherit it."""
    bot = _agent_bot(tmp_path)
    stopped_manager = MagicMock(spec=CallManager)
    await start_call_manager(bot, stopped_manager)
    await bot._post_join_room_setup("!call:localhost")
    bot.mark_sync_loop_started()

    await bot.stop()
    stopped_manager.shutdown.assert_awaited_once_with()
    restarted_manager = MagicMock(spec=CallManager)
    await start_call_manager(bot, restarted_manager)
    assert bot._runtime_view.resume()
    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await _complete_frame(bot)
    await _settle()

    stopped_manager.reconcile_joined_rooms.assert_not_awaited()
    restarted_manager.reconcile_joined_rooms.assert_not_awaited()

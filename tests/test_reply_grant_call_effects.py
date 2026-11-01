"""How grant changes reach real bots' calls and invites, and when each bot runs its call work (frames, drains, stops)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nio import TimelineEventProvenance

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.constants import ROUTER_AGENT_NAME
from tests.conftest import start_call_manager
from tests.test_bot_ready_hook import (
    _FRESH_SEMANTIC_FACTS,
    _REPLAY_FACTS,
    _agent_bot,
    _complete_frame,
    _grant_room_client,
    _member_event,
    _validated_reported_membership_admission,
    _validated_timeline_admission,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import AdmissionFacts
    from tests.runtime_fakes import ReadyRuntime

GRANT_ROOM_ID = "!grant:localhost"
JOINER = "@bob:localhost"


@dataclass
class _Calls:
    """What every bot's call manager was asked, in order, with a switch that holds revocations in progress."""

    log: list[tuple[str, str]] = field(default_factory=list)
    hold: asyncio.Event | None = None
    revocations_started: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(0))

    async def revoke(self, agent_name: str) -> None:
        self.log.append((agent_name, "revoke"))
        self.revocations_started.release()
        if self.hold is not None:
            await self.hold.wait()
        self.log.append((agent_name, "revoked"))

    def observe(self, agent_name: str, manager: MagicMock) -> MagicMock:
        """Log what ``manager`` is asked to revoke and reconcile, under ``agent_name``."""

        async def reconcile() -> None:
            self.log.append((agent_name, "reconcile"))

        manager.revoke_reply_authorization.side_effect = partial(self.revoke, agent_name)
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


async def _router_and_agent(tmp_path: Path, calls: _Calls) -> tuple[AgentBot, AgentBot, dict[str, MagicMock]]:
    """Return a router and an agent with logging call managers, the router's grant room refreshed and ready."""
    router = _agent_bot(tmp_path, agent_name=ROUTER_AGENT_NAME)
    agent = _agent_bot(tmp_path, agent_name="code", orchestrator=cast("ReadyRuntime", router.orchestrator))
    router.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    router.client = _grant_room_client(router, GRANT_ROOM_ID)
    await router._runtime_view.agent_reply_memberships.refresh(router.config, router.runtime_paths, router.client)
    managers = {bot.agent_name: calls.observe(bot.agent_name, await start_call_manager(bot)) for bot in (router, agent)}
    return router, agent, managers


async def _settle() -> None:
    assert await wait_for_background_tasks(timeout=1)


async def _admit_live_join(router: AgentBot, facts: AdmissionFacts = _FRESH_SEMANTIC_FACTS) -> None:
    """Admit, as the router's durable ingestion does, one LIVE join of ``JOINER`` into the grant room."""
    admission = _validated_timeline_admission(
        router,
        GRANT_ROOM_ID,
        _member_event("$join", user_id=JOINER, membership="join", ts=1),
    )
    await router._after_ingestion_admission(admission, facts, TimelineEventProvenance.LIVE)


@pytest.mark.asyncio
async def test_authoritative_refresh_revokes_every_bots_calls_before_reconciling_any(tmp_path: Path) -> None:
    """No call starts for a newly allowed requester until every bot finished ending denied calls."""
    hold = asyncio.Event()
    calls = _Calls(hold=hold)
    router, _agent, _managers = await _router_and_agent(tmp_path, calls)

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
async def test_live_grant_change_retries_every_bots_effects_after_a_failed_revocation(tmp_path: Path) -> None:
    """A revocation that fails reconciles nothing, and the replayed transition revokes and reconciles again."""
    calls = _Calls()
    router, _agent, managers = await _router_and_agent(tmp_path, calls)
    failing = managers["code"].revoke_reply_authorization
    revoke = failing.side_effect

    async def fail_once() -> None:
        if failing.await_count == 1:
            msg = "revocation interrupted"
            raise RuntimeError(msg)
        await revoke()

    failing.side_effect = fail_once

    with pytest.raises(RuntimeError, match="revocation interrupted"):
        await _admit_live_join(router)
    await _settle()
    assert calls.ops("reconcile") == []
    assert router._runtime_view.agent_reply_memberships.is_allowed(
        JOINER,
        ["grant"],
        router.config,
        router.runtime_paths,
    )

    await _admit_live_join(router, _REPLAY_FACTS)
    await _settle()

    assert calls.ops("revoked") == ["code", "router", "router"]
    assert calls.ops("reconcile") == ["code", "router"]


@pytest.mark.asyncio
async def test_full_call_reconciliation_covers_room_requests(tmp_path: Path) -> None:
    """A full reconciliation requested beside room-scoped ones runs once over every joined room."""
    bot = _agent_bot(tmp_path)
    manager = await start_call_manager(bot)

    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await bot._post_join_room_setup("!first:localhost")
        bot.mark_sync_loop_started()
        await bot._post_join_room_setup("!second:localhost")
        await _complete_frame(bot)
        await _settle()

    manager.reconcile_joined_rooms.assert_awaited_once_with()


@pytest.fixture
def _short_stop_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound each bot's stop drain so work it cancels is cancelled within the test."""
    monkeypatch.setattr("mindroom.bot.SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.3)


@dataclass
class _Held:
    """An awaited step held once started until released, recording whether it was cancelled or finished."""

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
) -> tuple[AgentBot, AgentBot, dict[str, MagicMock], _Held]:
    """Return a router and agent after a live grant change, the agent's positive call reconciliation held."""
    calls = _Calls()
    router, agent, managers = await _router_and_agent(tmp_path, calls)
    held = _Held()
    managers["code"].reconcile_reply_authorization.side_effect = held.run
    await asyncio.wait_for(_admit_live_join(router), timeout=1)
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

    held.release.set()
    assert await wait_for_background_tasks(timeout=1, owner=agent._runtime_view)
    assert held.finished.is_set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_stop_drain")
async def test_router_stop_leaves_another_bots_fence_revocation_running(tmp_path: Path) -> None:
    """A revocation a router fence scheduled for another agent completes even when the router stops first."""
    hold = asyncio.Event()
    calls = _Calls(hold=hold)
    router, agent, _managers = await _router_and_agent(tmp_path, calls)
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
    router, agent, _managers = await _router_and_agent(tmp_path, calls)
    agent.client = _grant_room_client(agent, GRANT_ROOM_ID)
    if draining:
        agent._runtime_view.begin_draining()

    with patch.object(agent._room_lifecycle, "reconcile_pending_invites", new=AsyncMock()) as invites:
        await _admit_live_join(router)
        await _settle()

    assert calls.ops("revoked") == ["code", "router"]
    assert calls.ops("reconcile") == (["router"] if draining else ["code", "router"])
    assert invites.await_count == (0 if draining else 1)

    router.invalidate_agent_reply_memberships(reason="uncertain_sync_response")
    await _settle()
    assert calls.ops("revoked") == ["code", "code", "router", "router"]


@pytest.mark.asyncio
async def test_call_reconciliation_waits_when_draining_begins_during_frame_effects(tmp_path: Path) -> None:
    """A drain that begins while a frame runs its side effects leaves pending call work for the next accepting frame."""
    bot = _agent_bot(tmp_path)
    manager = await start_call_manager(bot)
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
    stopped_manager = await start_call_manager(bot)
    await bot._post_join_room_setup("!call:localhost")
    bot.mark_sync_loop_started()

    await bot.stop()
    stopped_manager.shutdown.assert_awaited_once_with()
    restarted_manager = await start_call_manager(bot)
    assert bot._runtime_view.resume()
    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await _complete_frame(bot)
    await _settle()

    stopped_manager.reconcile_joined_rooms.assert_not_awaited()
    restarted_manager.reconcile_joined_rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bot_without_a_client_gets_no_invite_recheck(tmp_path: Path) -> None:
    """A bot that has not logged in has no invites to recheck, so a grant change schedules no invite work for it."""
    bot = _agent_bot(tmp_path)

    with patch.object(bot._room_lifecycle, "reconcile_pending_invites", new=AsyncMock()) as invites:
        bot.schedule_pending_invite_reconciliation()
        await _settle()

    invites.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_grant_change_does_not_wait_for_an_invite_join(tmp_path: Path) -> None:
    """A pending invite cannot make the router's ingestion pump await a join that needs that pump to progress."""
    router, _agent, _managers = await _router_and_agent(tmp_path, _Calls())
    join = _Held()

    with patch.object(router._room_lifecycle, "reconcile_pending_invites", new=AsyncMock(side_effect=join.run)):
        try:
            await asyncio.wait_for(_admit_live_join(router), timeout=1)
            await asyncio.wait_for(join.started.wait(), timeout=1)
        finally:
            join.release.set()
            await _settle()

"""Entity lifecycle owned by the fleet: start, retry, stop, removal, and process shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.orchestration import entity_fleet
from mindroom.orchestration.entity_fleet import EntityFleet, EntityFleetHooks, EntityStartResults
from mindroom.runtime_shutdown import ENTITY_REMOVED_SHUTDOWN, SYNC_RESTART_SHUTDOWN, ResponseShutdownTimeoutError
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config


@dataclass
class _Bot:
    """A bot that records each lifecycle call it receives."""

    agent_name: str
    order: list[str]
    start_errors: list[BaseException] = field(default_factory=list)
    prepare_error: BaseException | None = None
    leave_error: BaseException | None = None
    stop_error: BaseException | None = None
    running: bool = False
    client: object | None = None
    deferred_stop_required: bool = False
    pending_response_owner_count: int = 0
    pending_response_phase_counts: dict[str, int] = field(default_factory=dict)
    deferred_stop_phase: str | None = None

    async def start(self) -> None:
        self.order.append(f"start:{self.agent_name}")
        if self.start_errors:
            raise self.start_errors.pop(0)
        self.running = True
        self.client = object()

    async def _quiesce_matrix_ingestion(self) -> None:
        self.order.append(f"quiesce:{self.agent_name}")

    async def prepare_for_sync_shutdown(self, *, shutdown_intent: object) -> None:
        self.order.append(f"prepare:{self.agent_name}:{shutdown_intent}")
        if self.prepare_error is not None:
            raise self.prepare_error

    async def leave_rooms(self) -> None:
        if self.client is None:
            msg = "Matrix client is not ready for room lifecycle work"
            raise RuntimeError(msg)
        self.order.append(f"leave:{self.agent_name}")
        if self.leave_error is not None:
            raise self.leave_error

    async def stop(self, *, shutdown_intent: object) -> None:
        self.order.append(f"stop:{self.agent_name}:{shutdown_intent}")
        self.running = False
        if self.stop_error is not None:
            raise self.stop_error

    def begin_process_shutdown(self) -> None:
        self.order.append(f"begin_shutdown:{self.agent_name}")

    async def finish_deferred_stop(self, *, shutdown_intent: object, timeout_seconds: float) -> None:
        del shutdown_intent, timeout_seconds
        self.order.append(f"finish_deferred:{self.agent_name}")
        self.deferred_stop_required = False
        self.pending_response_owner_count = 0


@dataclass
class _Harness:
    order: list[str] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    unresolved: set[str] = field(default_factory=set)
    started: list[EntityStartResults] = field(default_factory=list)
    built: dict[str, _Bot] = field(default_factory=dict)
    fleet: EntityFleet = field(init=False)

    def __post_init__(self) -> None:
        async def create_bots(names: set[str], _config: Config) -> dict[str, _Bot]:
            return {name: self.built.setdefault(name, _Bot(name, self.order)) for name in names}

        def entities_changing(names: set[str]) -> None:
            self.order.append(f"changing:{','.join(sorted(names))}")

        async def entities_started(results: EntityStartResults) -> None:
            self.started.append(results)

        async def setup_rooms_after_retry(name: str) -> None:
            self.order.append(f"rooms:{name}")

        async def reconcile_removed(names: set[str]) -> set[str]:
            self.order.append(f"reconcile:{','.join(sorted(names))}")
            return self.unresolved & names

        async def end_replies(names: set[str]) -> None:
            self.order.append(f"end_replies:{','.join(sorted(names))}")

        self.fleet = EntityFleet(
            EntityFleetHooks(
                create_bots=create_bots,
                start_blocked=lambda names: self.blocked & names,
                entities_changing=entities_changing,
                entities_started=entities_started,
                setup_rooms_after_retry=setup_rooms_after_retry,
                reconcile_removed=reconcile_removed,
                end_replies=end_replies,
            ),
        )

    def add(self, name: str, **fields: object) -> _Bot:
        bot = _Bot(name, self.order, **fields)
        self.fleet.add(name, bot)
        return bot


@pytest.fixture
def fake_sync(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the Matrix sync loop with one that runs until cancelled and records its stop."""
    events: list[str] = []

    async def sync_forever(bot: _Bot) -> None:
        events.append(f"sync:{bot.agent_name}")
        try:
            await asyncio.Event().wait()
        finally:
            bot.order.append(f"cancel_sync:{bot.agent_name}")

    monkeypatch.setattr(entity_fleet, "sync_forever_with_restart", sync_forever)
    return events


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry background starts immediately."""
    monkeypatch.setattr(entity_fleet, "retry_delay_seconds", lambda *_args, **_kwargs: 0)


async def _settle(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110 - the fleet exposes no event for these internal steps
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_removal_settles_approvals_with_the_bot_present_then_leaves_stops_and_ends_replies(
    fake_sync: list[str],
) -> None:
    """A removed entity keeps its live sender and sync source until its approvals and rooms are settled."""
    harness = _Harness()
    harness.add("gone")
    await harness.fleet.start({"gone"})
    harness.order.clear()

    await harness.fleet.remove({"gone"})

    assert harness.order == [
        "changing:gone",
        f"prepare:gone:{ENTITY_REMOVED_SHUTDOWN}",
        "reconcile:gone",
        "leave:gone",
        "cancel_sync:gone",
        f"stop:gone:{ENTITY_REMOVED_SHUTDOWN}",
        "end_replies:gone",
    ]
    assert "gone" not in harness.fleet.bots
    assert fake_sync == ["sync:gone"]


@pytest.mark.asyncio
async def test_removal_keeps_rooms_while_approval_recovery_is_unresolved(fake_sync: list[str]) -> None:
    """An unresolved frozen FINAL keeps its rooms so the recovery retry can still deliver there."""
    del fake_sync
    harness = _Harness(unresolved={"gone"})
    harness.add("gone")
    harness.add("other")
    await harness.fleet.start({"gone", "other"})
    harness.order.clear()

    await harness.fleet.remove({"gone", "other"})

    assert "leave:gone" not in harness.order
    assert "leave:other" in harness.order
    assert "end_replies:gone,other" in harness.order
    assert not harness.fleet.bots


@pytest.mark.asyncio
async def test_failed_removal_preparation_never_leaves_stops_or_ends_replies(fake_sync: list[str]) -> None:
    """A removal step that fails stops that entity's removal before anything irreversible."""
    del fake_sync
    harness = _Harness()
    harness.add("broken", prepare_error=RuntimeError("drain failed"))
    harness.add("fine")
    await harness.fleet.start({"broken", "fine"})
    harness.order.clear()

    with pytest.raises(RuntimeError, match="drain failed"):
        await harness.fleet.remove({"broken", "fine"})

    assert "reconcile:fine" in harness.order
    assert not [entry for entry in harness.order if entry.startswith(("leave:broken", "stop:broken"))]
    assert "end_replies:fine" in harness.order
    assert set(harness.fleet.bots) == {"broken"}


@pytest.mark.asyncio
async def test_restart_stop_quiesces_then_cancels_sync_before_preparing(fake_sync: list[str]) -> None:
    """Sync loops stop before callback drains are certified, so no callback appears after the decision."""
    del fake_sync
    harness = _Harness()
    harness.add("agent")
    await harness.fleet.start({"agent"})
    harness.order.clear()

    await harness.fleet.stop({"agent"})

    assert harness.order == [
        "changing:agent",
        "quiesce:agent",
        "cancel_sync:agent",
        f"prepare:agent:{SYNC_RESTART_SHUTDOWN}",
        f"stop:agent:{SYNC_RESTART_SHUTDOWN}",
    ]
    assert not harness.fleet.bots


@pytest.mark.asyncio
async def test_restart_tolerates_only_a_reply_that_outlived_the_drain(fake_sync: list[str]) -> None:
    """A restart replaces a bot whose only failure is a reply its replacement replays; any other failure wins."""
    del fake_sync
    harness = _Harness()
    harness.add("timed_out", stop_error=ResponseShutdownTimeoutError("reply outlived the drain"))
    await harness.fleet.stop({"timed_out"})
    assert not harness.fleet.bots

    harness.add(
        "failed",
        prepare_error=ResponseShutdownTimeoutError("reply outlived the drain"),
        stop_error=RuntimeError("store did not close"),
    )
    with pytest.raises(RuntimeError, match="store did not close"):
        await harness.fleet.stop({"failed"})
    assert set(harness.fleet.bots) == {"failed"}


@pytest.mark.asyncio
async def test_start_classifies_permanent_transient_and_blocked_entities(
    fake_sync: list[str],
    no_retry_delay: None,
) -> None:
    """A permanent failure disables the entity, a transient one retries, a blocked one waits for its server."""
    del no_retry_delay
    harness = _Harness(blocked={"blocked"})
    harness.add("ok")
    harness.add("broken", start_errors=[PermanentStartupError("bad config")])
    flaky = harness.add("flaky", start_errors=[RuntimeError("homeserver busy")])
    harness.add("blocked")

    results = await harness.fleet.start({"ok", "broken", "flaky", "blocked"})

    assert [bot.agent_name for bot in results.started_bots] == ["ok"]
    assert results.permanently_failed_entities == ["broken"]
    assert sorted(results.retryable_entities) == ["blocked", "flaky"]
    assert harness.fleet.is_disabled("broken")
    assert not harness.fleet.is_disabled("flaky")
    assert harness.started[0] is results
    await _settle(lambda: "rooms:flaky" in harness.order and "sync:flaky" in fake_sync)
    assert flaky.running
    assert [bot.agent_name for result in harness.started[1:] for bot in result.started_bots] == ["flaky"]
    assert "start:blocked" not in harness.order
    await harness.fleet.cancel_retries()


@pytest.mark.asyncio
async def test_background_retry_waits_before_its_first_attempt_and_stops_on_cancel(fake_sync: list[str]) -> None:
    """A retry only follows a failed attempt, so it waits first, and stopping the entity cancels it."""
    del fake_sync
    harness = _Harness()
    harness.add("flaky", start_errors=[RuntimeError("down"), RuntimeError("still down")])
    await harness.fleet.start({"flaky"})

    await asyncio.sleep(0)
    assert harness.order.count("start:flaky") == 1

    await harness.fleet.stop({"flaky"})
    assert harness.order.count("start:flaky") == 1
    assert not harness.fleet.bots


@pytest.mark.asyncio
async def test_replace_rebuilds_and_starts_entities_on_the_new_config(fake_sync: list[str]) -> None:
    """Replacement stops the old generation, builds a new bot, and starts it."""
    del fake_sync
    harness = _Harness()
    old = harness.add("agent")
    await harness.fleet.start({"agent"})

    results = await harness.fleet.replace({"agent"}, AsyncMock())

    new = harness.fleet.bots["agent"]
    assert new is not old
    assert results.started_bots == [new]
    assert new.running


@pytest.mark.asyncio
async def test_process_shutdown_finishes_deferred_stops_and_reports_remaining_owners(fake_sync: list[str]) -> None:
    """Shutdown keeps every slot and reports owners that still hold the shared stores."""
    del fake_sync
    harness = _Harness()
    deferred = harness.add(
        "deferred",
        deferred_stop_required=True,
        pending_response_owner_count=1,
        stop_error=ResponseShutdownTimeoutError("reply still running"),
    )
    harness.add("clean")
    await harness.fleet.start({"deferred", "clean"})

    shutdown = await harness.fleet.stop_all()

    assert shutdown.failures == []
    assert shutdown.owns_resources is False
    assert "finish_deferred:deferred" in harness.order
    assert deferred.deferred_stop_required is False
    assert set(harness.fleet.bots) == {"deferred", "clean"}


@pytest.mark.asyncio
async def test_cancelling_a_start_stuck_in_login_ends_it_without_another_attempt() -> None:
    """Ctrl+C during a start attempt cancels it; nothing retries a cancelled start."""
    harness = _Harness()
    entered = asyncio.Event()
    bot = harness.add("stuck")

    async def blocked_start() -> None:
        entered.set()
        await asyncio.Event().wait()

    bot.start = blocked_start
    start = asyncio.create_task(harness.fleet.start({"stuck"}))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert harness.started == []
    assert harness.fleet.bots["stuck"] is bot


@pytest.mark.asyncio
async def test_removal_ends_replies_only_for_entities_whose_bot_stopped(fake_sync: list[str]) -> None:
    """A removal whose stop failed keeps its slot, and its replies stay open for the bot that still owns them."""
    del fake_sync
    harness = _Harness()
    harness.add("stuck", stop_error=ResponseShutdownTimeoutError("reply still running"))
    harness.add("done")
    await harness.fleet.start({"stuck", "done"})

    with pytest.raises(ResponseShutdownTimeoutError):
        await harness.fleet.remove({"stuck", "done"})

    assert "end_replies:done" in harness.order
    assert set(harness.fleet.bots) == {"stuck"}


@pytest.mark.asyncio
async def test_removing_a_bot_that_never_started_skips_leaving_and_still_removes_it(fake_sync: list[str]) -> None:
    """A bot held back by an unavailable required MCP server has no client; removing it must not fail the reload."""
    del fake_sync
    harness = _Harness(blocked={"held"})
    harness.add("held")
    await harness.fleet.start({"held"})
    await harness.fleet.cancel_retries()

    assert await harness.fleet.remove({"held"}) == set()

    assert "leave:held" not in harness.order
    assert f"stop:held:{ENTITY_REMOVED_SHUTDOWN}" in harness.order
    assert "end_replies:held" in harness.order
    assert not harness.fleet.bots

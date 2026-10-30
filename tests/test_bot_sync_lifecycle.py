"""Bot sync lifecycle: startup cleanup, checkpoint certification, and background drains.

What is left here after the advisory event cache was deleted. Everything this
file used to assert about cached sync timelines went with the writer that
produced them; these tests are about the bot's own lifecycle, which the cache
only ever happened to share a sync callback with.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.bot_runtime_view import RunPhase
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, current_task_is_process_shutdown
from mindroom.config.plugin import PluginEntryConfig
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import EventClass, EventKind
from mindroom.hooks import EVENT_AGENT_STARTED, HookRegistry, hook
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN, SYNC_RESTART_SHUTDOWN, ShutdownBudget
from mindroom.turn_record import TurnRecord
from tests.journal_helpers import admit_dispatch_event
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _make_client_mock,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


class TestBotSyncLifecycle(ThreadingBehaviorTestBase):
    """Startup, checkpoint certification, redaction ownership, and drain behavior."""

    @pytest.mark.asyncio
    async def test_start_holds_reactions_until_semantic_dispatch_is_released(self, bot: AgentBot) -> None:
        """Durable non-turn work must survive startup without running under stale grants."""
        dispatched = asyncio.Event()

        async def on_reaction(_room: nio.MatrixRoom, _event: nio.ReactionEvent) -> TurnDispatchOutcome:
            dispatched.set()
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED

        dispatcher = bot._journal_dispatcher
        dispatcher.callbacks = replace(dispatcher.callbacks, on_reaction=on_reaction)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = nio.ReactionEvent.from_dict(
            {
                "type": "m.reaction",
                "event_id": "$startup-reaction:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$target", "key": "ok"}},
            },
        )
        assert isinstance(event, nio.ReactionEvent)
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch.object(bot, "_open_owned_matrix_client", AsyncMock(return_value=bot.client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "refresh_presence", AsyncMock()),
            patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
        ):
            await bot.start()

        try:
            await admit_dispatch_event(dispatcher, room, event, EventKind.REACTION, EventClass.ACTIONABLE)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(dispatched.wait(), timeout=0.05)
            assert await dispatcher.store.is_pending(event.event_id)

            bot.release_pending_turn_journal_replay()
            await asyncio.wait_for(dispatched.wait(), timeout=1.0)
            await dispatcher.drain_once()
            assert not await dispatcher.store.is_pending(event.event_id)
        finally:
            await dispatcher.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "startup_error",
        [RuntimeError("hook boom"), asyncio.CancelledError()],
        ids=["error", "cancel"],
    )
    async def test_start_resets_running_flag_when_agent_started_hooks_fail(
        self,
        bot: AgentBot,
        startup_error: BaseException,
    ) -> None:
        """Startup cleanup should clear running state if EVENT_AGENT_STARTED emission fails."""
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()
        ingestion_session = AsyncMock()
        close_order: list[str] = []
        cleanup_error = RuntimeError("ingestion cleanup boom")

        async def close_ingestion() -> None:
            close_order.append("ingestion")
            raise cleanup_error

        async def close_http() -> None:
            close_order.append("http")

        ingestion_session.close.side_effect = close_ingestion
        start_client.close.side_effect = close_http
        hook_registry = MagicMock()
        hook_registry.has_hooks.side_effect = lambda event_name: event_name == EVENT_AGENT_STARTED
        bot.orchestrator.hook_registry = hook_registry

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch(
                "mindroom.bot.login_agent_owned_session",
                AsyncMock(return_value=SimpleNamespace(client=start_client, session=ingestion_session)),
            ),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "refresh_presence", AsyncMock()),
            patch("mindroom.bot.emit", AsyncMock(side_effect=startup_error)),
            pytest.raises(type(startup_error)),
        ):
            await bot.start()

        start_client.close.assert_awaited_once()
        ingestion_session.close.assert_awaited_once()
        assert close_order == ["ingestion", "http"]
        assert bot.running is False
        assert bot.client is None
        assert bot._ingestion_session is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["lookup", "write", "timeout", "system_exit"])
    async def test_required_startup_hook_failure_stops_before_room_reconciliation(
        self,
        bot: AgentBot,
        failure: str,
    ) -> None:
        """Failed room ownership initialization must close startup before cleanup."""
        later_hook = AsyncMock()

        @hook(EVENT_AGENT_STARTED, priority=10, timeout_ms=5, required=True)
        async def retain_rooms(_ctx: object) -> None:
            if failure == "timeout":
                await asyncio.sleep(60)
            if failure == "system_exit":
                raise SystemExit(1)
            msg = f"retention {failure} failed"
            raise OSError(msg)

        @hook(EVENT_AGENT_STARTED, priority=20)
        async def backfill(_ctx: object) -> None:
            await later_hook()

        bot.orchestrator.hook_registry = HookRegistry.from_plugins(
            [
                SimpleNamespace(
                    name="room-owner",
                    discovered_hooks=(retain_rooms, backfill),
                    discovered_automations=(),
                    plugin_order=0,
                    entry_config=PluginEntryConfig(path="./plugins/room-owner"),
                ),
            ],
        )
        client = _make_client_mock(user_id="@mindroom_general:localhost")
        session = AsyncMock()
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch(
                "mindroom.bot.login_agent_owned_session",
                AsyncMock(return_value=SimpleNamespace(client=client, session=session)),
            ),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "refresh_presence", AsyncMock()),
            pytest.raises(RuntimeError, match="Required startup hook") as error,
        ):
            await bot.start()

        expected_cause = {"timeout": TimeoutError, "system_exit": SystemExit}.get(failure, OSError)
        assert isinstance(error.value.__cause__, expected_cause)
        later_hook.assert_not_awaited()
        client.close.assert_awaited_once()
        session.close.assert_awaited_once()
        assert bot.client is None
        assert not bot.running

    @pytest.mark.asyncio
    async def test_approval_recovery_retains_owned_store_until_final_send_finishes(self, bot: AgentBot) -> None:
        """A recovery-only FINAL reuses restored owned state and releases both ownership lanes."""
        bot.client = None
        client = _make_client_mock(user_id=bot.agent_user.user_id)
        client.device_id = "RECOVERY_DEVICE"
        session = AsyncMock()
        order: list[str] = []

        async def recover(_approval_id: str) -> bool:
            assert bot.client is client
            assert bot._ingestion_session is session
            assert bot._sending_device_id == "RECOVERY_DEVICE"
            order.append("final")
            return True

        async def close_session() -> None:
            order.append("session")

        async def close_client() -> None:
            order.append("client")

        session.close.side_effect = close_session
        client.close.side_effect = close_client
        with (
            patch(
                "mindroom.bot.login_agent_owned_session",
                AsyncMock(return_value=SimpleNamespace(client=client, session=session)),
            ),
            patch.object(bot._response_runner, "recover_approval_final", AsyncMock(side_effect=recover)),
        ):
            assert await bot.recover_approval_final("approval")

        assert order == ["final", "session", "client"]
        assert bot.client is None
        assert bot._ingestion_session is None
        assert bot._sending_device_id is None
        client.sync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_login_as_another_user_keeps_the_runtime_journal(self, bot: AgentBot) -> None:
        """A re-login under a different Matrix ID moves the principal, not the database.

        The bot borrows its runtime's one store, and the rebuild that follows an
        identity change borrows that same store again. One database holds every
        principal, and turn records are deliberately not principal-scoped
        precisely so that a re-login keeps reading the same database. The bot's
        stop leaves the store open for the other bots of its runtime.
        """
        store_before_login = bot._journal_store
        assert store_before_login is bot.orchestrator.event_journal_store()
        identity_before_login = bot.matrix_id

        bot.agent_user.user_id = "@mindroom_general_2:localhost"
        bot._rebuild_runtime_components_after_login_if_identity_changed(identity_before_login)

        assert bot._journal_principal_id == "general@@mindroom_general_2:localhost"
        assert bot._journal_store is store_before_login

        await bot.stop()

        await store_before_login.existing_generation()

    def test_a_rebuild_wires_each_collaborator_to_the_owners_of_its_own_build(self, bot: AgentBot) -> None:
        """Collaborators built after the owners they hand work to hold those owners, never ones from an earlier build."""
        identity_before_login = bot.matrix_id
        first_dispatcher = bot._journal_dispatcher

        bot.agent_user.user_id = "@mindroom_general_2:localhost"
        bot._rebuild_runtime_components_after_login_if_identity_changed(identity_before_login)

        dispatcher, runner, gate = bot._journal_dispatcher, bot._response_runner, bot._coalescing_gate
        assert dispatcher is not first_dispatcher
        assert bot._turn_store.deps.retry_dispatch_sources == dispatcher.retry_turn_sources
        assert bot._reply_runtime.retry_sources == dispatcher.retry_turn_sources
        assert bot._delivery_gateway.deps.turn_handoff == dispatcher.release_delivered_turn_sources
        assert (
            bot._ingress_validator.deps.settle_ignored_sources == dispatcher.settle_intentionally_ignored_turn_sources
        )
        assert gate._dispatch_turn == bot._turn_dispatcher.handle_prepared_turn
        assert gate._on_dispatch_failure == dispatcher.retry_turn_sources
        assert gate._on_undelivered_source == dispatcher.retry_turn_source
        assert gate._on_intentionally_ignored_sources == dispatcher.settle_intentionally_ignored_turn_sources
        assert gate._wait_for_thread_response_idle == runner.wait_for_thread_response_idle
        assert gate._active_thread_ids_for_room == runner.active_thread_ids_for_room
        assert gate._room_scope_is_single_conversation == bot._conversation_resolver.room_is_one_conversation
        # The journal's schedule trigger trusts exactly the senders the entity registry manages.
        assert dispatcher.schedule_trigger_sender_is_managed(identity_before_login.full_id)
        assert not dispatcher.schedule_trigger_sender_is_managed("@user:localhost")

    @pytest.mark.asyncio
    async def test_live_redaction_callback_only_runs_what_its_admission_ended(self, bot: AgentBot) -> None:
        """Admission already recorded and applied the redaction, so the callback owes only the ended replies' effects."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_agent:localhost")
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.redacts = "$source:localhost"
        ended = object()

        with (
            patch.object(bot._reply_runtime, "deletions_ended", AsyncMock(return_value=ended)),
            patch.object(bot, "_replies_ended") as replies_ended,
        ):
            await bot._on_redaction(room, redaction_event)

        replies_ended.assert_called_once_with(ended)
        assert not await bot._turn_store.redacted_event_ids(room.room_id, ("$source:localhost",))

    @pytest.mark.asyncio
    async def test_running_bot_prunes_stale_handled_turns_without_a_restart(self, bot: AgentBot) -> None:
        """Turn retention keeps applying while sync runs, not only at startup."""
        await bot._turn_store.warm()
        now = time.time()
        with patch("mindroom.event_journal.turn_rows.time.time", return_value=now - 40 * 24 * 60 * 60):
            await bot._turn_store.record_turn(TurnRecord.create(["$old:localhost"]), room_id="!test:localhost")
        await bot._turn_store.record_turn(TurnRecord.create(["$recent:localhost"]), room_id="!test:localhost")

        async def publish_frame() -> None:
            await bot._run_sync_response_side_effects(first_sync_response=False)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        with (
            patch.object(bot, "_schedule_delivery_recovery"),
        ):
            await publish_frame()
            assert await bot._turn_store.get_turn_record("$old:localhost") is not None

            bot._next_handled_turn_cleanup_at = 0.0
            await publish_frame()

        assert await bot._turn_store.get_turn_record("$old:localhost") is None
        assert await bot._turn_store.get_turn_record("$recent:localhost") is not None

    @pytest.mark.asyncio
    async def test_handled_turn_cleanup_stops_at_sync_shutdown(self, bot: AgentBot) -> None:
        """Shutdown cancels a running retention pass and schedules no further ones."""
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_cleanup(**_kwargs: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with (
            patch.object(bot, "_schedule_delivery_recovery"),
            patch.object(bot._turn_store, "cleanup", side_effect=blocked_cleanup) as cleanup,
        ):
            bot._next_handled_turn_cleanup_at = 0.0
            await bot._run_sync_response_side_effects(first_sync_response=False)
            await asyncio.wait_for(started.wait(), timeout=1.0)

            bot._sync_shutdown_budget = ShutdownBudget.start(0.01)
            await asyncio.wait_for(bot.prepare_for_sync_shutdown(), timeout=2.0)
            assert cancelled.is_set()

            bot._next_handled_turn_cleanup_at = 0.0
            await bot._run_sync_response_side_effects(first_sync_response=False)
            assert await wait_for_background_tasks(timeout=0.2, owner=bot._runtime_view)

        assert cleanup.await_count == 1

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_owner_scope_isolated(self, bot: AgentBot) -> None:
        """Scoped waits should not block on background tasks owned by another bot."""
        other_owner = object()
        other_task_started = asyncio.Event()
        release_other_task = asyncio.Event()

        async def other_owner_task() -> None:
            other_task_started.set()
            await release_other_task.wait()

        other_task = create_background_task(
            other_owner_task(),
            name="other_owner_task",
            owner=other_owner,
        )

        await asyncio.wait_for(other_task_started.wait(), timeout=1.0)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
        assert not other_task.done()

        release_other_task.set()
        await wait_for_background_tasks(timeout=1.0, owner=other_owner)
        assert other_task.done()

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_drains_child_tasks_created_during_wait(self) -> None:
        """Owner-scoped draining should keep waiting for child tasks spawned by awaited tasks."""
        owner = object()
        parent_started = asyncio.Event()
        release_parent = asyncio.Event()
        child_started = asyncio.Event()
        release_child = asyncio.Event()
        child_finished = asyncio.Event()

        async def child_task() -> None:
            child_started.set()
            await release_child.wait()
            child_finished.set()

        async def parent_task() -> None:
            parent_started.set()
            await release_parent.wait()
            create_background_task(child_task(), name="child_task", owner=owner)

        parent = create_background_task(parent_task(), name="parent_task", owner=owner)
        await asyncio.wait_for(parent_started.wait(), timeout=1.0)

        drain_task = asyncio.create_task(wait_for_background_tasks(timeout=1.0, owner=owner))
        await asyncio.sleep(0)

        release_parent.set()
        await asyncio.wait_for(child_started.wait(), timeout=1.0)
        assert drain_task.done() is False

        release_child.set()
        await drain_task

        assert parent.done()
        assert child_finished.is_set()

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_timeout_stops_after_bounded_cancel_rounds(self) -> None:
        """Timed-out draining should return even if cancelled tasks keep spawning replacements."""
        owner = object()
        respawned_count = 0
        respawned_replacement = asyncio.Event()
        allow_respawn = True

        async def respawning_task() -> None:
            nonlocal respawned_count
            try:
                await asyncio.Future()
            finally:
                if allow_respawn:
                    respawned_count += 1
                    respawned_replacement.set()
                    create_background_task(
                        respawning_task(),
                        name=f"respawning_task_{respawned_count}",
                        owner=owner,
                    )

        create_background_task(respawning_task(), name="respawning_task_root", owner=owner)

        try:
            await asyncio.wait_for(wait_for_background_tasks(timeout=0.01, owner=owner), timeout=0.5)
            await asyncio.wait_for(respawned_replacement.wait(), timeout=0.5)
            assert respawned_count >= 1
        finally:
            allow_respawn = False
            await wait_for_background_tasks(timeout=0.05, owner=owner)

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_timeout_returns_when_task_suppresses_cancel(self) -> None:
        """Timed-out draining should not hang on a task that ignores cancellation."""
        owner = object()
        task_started = asyncio.Event()
        release_task = asyncio.Event()
        cancel_count = 0

        async def stubborn_task() -> None:
            nonlocal cancel_count
            task_started.set()
            while not release_task.is_set():
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancel_count += 1
                    if release_task.is_set():
                        raise

        task = create_background_task(stubborn_task(), name="stubborn_task", owner=owner)
        await asyncio.wait_for(task_started.wait(), timeout=1.0)

        try:
            completed = await asyncio.wait_for(
                wait_for_background_tasks(timeout=0.0, owner=owner),
                timeout=1.0,
            )
            assert completed is False
            assert cancel_count >= 1
        finally:
            release_task.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_timeout_preserves_shutdown_intent(self) -> None:
        """Timed-out owner task cancellation should preserve shutdown provenance."""
        owner = object()
        task_started = asyncio.Event()
        cancelled_args: list[tuple[object, ...]] = []

        async def never_finishes() -> None:
            task_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                cancelled_args.append(exc.args)
                raise

        create_background_task(never_finishes(), name="sync_restart_cancelled_task", owner=owner)
        await asyncio.wait_for(task_started.wait(), timeout=1.0)

        completed = await wait_for_background_tasks(
            timeout=0.0,
            owner=owner,
            shutdown_intent=SYNC_RESTART_SHUTDOWN,
        )

        assert completed is False
        assert cancelled_args == [(SYNC_RESTART_CANCEL_MSG,)]

    @pytest.mark.asyncio
    async def test_orderly_background_task_drain_marks_process_shutdown(self) -> None:
        """Owned recovery tasks must see process shutdown before their cancellation."""
        owner = object()
        task_started = asyncio.Event()
        process_shutdown_markers: list[bool] = []

        async def never_finishes() -> None:
            task_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                process_shutdown_markers.append(current_task_is_process_shutdown())
                raise

        create_background_task(never_finishes(), name="orderly_cancelled_task", owner=owner)
        await asyncio.wait_for(task_started.wait(), timeout=1.0)

        completed = await wait_for_background_tasks(
            timeout=0.0,
            owner=owner,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )

        assert completed is False
        assert process_shutdown_markers == [True]


class TestBotRunPhase(ThreadingBehaviorTestBase):
    """Which work a bot generation accepts across restart drains, process shutdown, and ingestion quiesce."""

    @staticmethod
    async def _accepts_waiting_responses(bot: AgentBot) -> bool:
        return await asyncio.wait_for(bot._runtime_view.wait_for_admission_or_shutdown(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_a_restart_drain_refuses_waiting_responses_until_the_next_sync_loop(self, bot: AgentBot) -> None:
        """A sync restart refuses work waiting for admission but keeps owned responses, and the next loop reopens."""
        assert await self._accepts_waiting_responses(bot)
        bot._sync_shutdown_budget = ShutdownBudget.start(0.01)

        await asyncio.wait_for(bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN), timeout=2.0)

        assert not await self._accepts_waiting_responses(bot)
        # Owned and inbox responses still drain; only a process shutdown refuses them.
        assert bot._runtime_view.phase is RunPhase.DRAINING
        bot.mark_sync_loop_started()
        assert await self._accepts_waiting_responses(bot)

    @pytest.mark.asyncio
    async def test_a_drain_refuses_a_response_already_waiting_for_admission(self, bot: AgentBot) -> None:
        """A response waiting while a config apply holds admission closed is refused when a drain begins."""
        gate = bot.orchestrator.response_admission_gate
        gate.close()
        try:
            waiting = asyncio.create_task(bot._runtime_view.wait_for_admission_or_shutdown())
            await asyncio.sleep(0)
            assert not waiting.done()
            bot._sync_shutdown_budget = ShutdownBudget.start(0.01)
            await asyncio.wait_for(bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN), timeout=2.0)
            assert await asyncio.wait_for(waiting, timeout=1.0) is False
        finally:
            gate.reopen()

    @pytest.mark.asyncio
    async def test_a_response_waiting_for_admission_enters_once_the_apply_reopens_it(self, bot: AgentBot) -> None:
        """A response that waited out a config apply is admitted when the gate reopens."""
        gate = bot.orchestrator.response_admission_gate
        gate.close()
        waiting = asyncio.create_task(bot._runtime_view.wait_for_admission_or_shutdown())
        await asyncio.sleep(0)
        assert not waiting.done()
        gate.reopen()
        assert await asyncio.wait_for(waiting, timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_a_drain_after_process_shutdown_keeps_the_generation_shut_down(self, bot: AgentBot) -> None:
        """A stop that drains once process shutdown began never turns the shutdown back into a restart drain."""
        bot.begin_process_shutdown()
        bot._sync_shutdown_budget = ShutdownBudget.start(0.01)

        await asyncio.wait_for(bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN), timeout=2.0)

        assert bot._runtime_view.phase is RunPhase.SHUTTING_DOWN

    @pytest.mark.asyncio
    async def test_process_shutdown_refuses_everything_and_no_sync_loop_reopens_it(self, bot: AgentBot) -> None:
        """Orderly process shutdown closes admission for good, including new inbox responses."""
        bot.begin_process_shutdown()

        assert not await self._accepts_waiting_responses(bot)
        assert bot._runtime_view.phase is RunPhase.SHUTTING_DOWN
        bot.mark_sync_loop_started()
        assert not await self._accepts_waiting_responses(bot)
        assert bot._runtime_view.phase is RunPhase.SHUTTING_DOWN

    @pytest.mark.asyncio
    async def test_a_quiesced_generation_stays_closed_after_its_drain(self, bot: AgentBot) -> None:
        """Once the fleet quiesces a bot's ingestion before stopping it, a late sync loop cannot reopen admission."""
        bot._ingestion_session = None
        await bot.quiesce_matrix_ingestion()
        bot._sync_shutdown_budget = ShutdownBudget.start(0.01)
        await asyncio.wait_for(bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN), timeout=2.0)

        bot.mark_sync_loop_started()

        assert not await self._accepts_waiting_responses(bot)

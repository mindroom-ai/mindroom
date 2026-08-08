"""Bot sync lifecycle: startup cleanup, checkpoint certification, and background drains.

What is left here after the advisory event cache was deleted. Everything this
file used to assert about cached sync timelines went with the writer that
produced them; these tests are about the bot's own lifecycle, which the cache
only ever happened to share a sync callback with.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG
from mindroom.hooks import EVENT_AGENT_STARTED
from mindroom.matrix.sync_certification import SyncTrustState
from mindroom.matrix.sync_token_values import SyncCheckpoint
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from tests.sync_continuity_helpers import load_sync_checkpoint
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _load_sync_token_value,
    _make_client_mock,
    _save_certified_sync_token,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


class TestBotSyncLifecycle(ThreadingBehaviorTestBase):
    """Startup, checkpoint certification, redaction ownership, and drain behavior."""

    @pytest.mark.asyncio
    async def test_start_resets_running_flag_when_agent_started_hooks_fail(self, bot: AgentBot) -> None:
        """Startup cleanup should clear running state if EVENT_AGENT_STARTED emission fails."""
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()
        bot.hook_registry = MagicMock()
        bot.hook_registry.has_hooks.side_effect = lambda event_name: event_name == EVENT_AGENT_STARTED

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=start_client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
            patch("mindroom.bot.emit", AsyncMock(side_effect=RuntimeError("hook boom"))),
            pytest.raises(RuntimeError, match="hook boom"),
        ):
            await bot.start()

        start_client.close.assert_awaited_once()
        assert bot.running is False
        assert bot.client is None

    @pytest.mark.asyncio
    async def test_a_login_as_another_user_keeps_the_journal_this_bot_already_opened(self, bot: AgentBot) -> None:
        """A re-login under a different Matrix ID moves the principal, not the database.

        This bot was built without a store handed to it, so it opened its own,
        and the rebuild that follows an identity change runs that same
        constructor step a second time. One database holds every principal, so
        the new identity's view comes from the store that is already open --
        and turn records are deliberately not principal-scoped precisely so
        that a re-login keeps reading the same database. Opening a second store
        would abandon the first with nobody left to close it: ``stop`` closes
        the handle the bot is holding, which would be the replacement.
        """
        store_before_login = bot._journal_store
        identity_before_login = bot.matrix_id

        bot.agent_user.user_id = "@mindroom_general_2:localhost"
        bot._rebuild_runtime_components_after_login_if_identity_changed(identity_before_login)

        assert bot._journal_principal_id == "general@@mindroom_general_2:localhost"
        assert bot._journal_store is store_before_login

        await bot.stop()

        with pytest.raises(RuntimeError, match="The event-journal store is closed"):
            await store_before_login.existing_generation()

    @pytest.mark.asyncio
    async def test_restored_first_sync_success_updates_checkpoint(self, bot: AgentBot) -> None:
        """Successful restored-token catch-up should save the new checkpoint token."""
        _save_certified_sync_token(bot, "s_before_complete")
        bot._runtime_view.mark_runtime_started()
        bot._sync_checkpoint_trust.state = SyncTrustState.PENDING
        bot.client.next_batch = "s_after_complete"

        await self._run_sync_response_without_startup_side_effects(bot, self._sync_response({}))

        checkpoint = load_sync_checkpoint(bot.storage_path, bot.agent_name)
        assert checkpoint is not None
        assert checkpoint.token == "s_after_complete"  # noqa: S105

    @pytest.mark.asyncio
    async def test_empty_joined_rooms_first_sync_certifies_checkpoint(self, bot: AgentBot) -> None:
        """A non-limited empty sync response can certify that there were no room deltas."""
        _save_certified_sync_token(bot, "s_before_empty")
        bot._runtime_view.mark_runtime_started()
        bot._sync_checkpoint_trust.state = SyncTrustState.PENDING
        bot.client.next_batch = "s_after_empty"

        await self._run_sync_response_without_startup_side_effects(bot, self._sync_response({}))

        checkpoint = load_sync_checkpoint(bot.storage_path, bot.agent_name)
        assert checkpoint is not None
        assert checkpoint.token == "s_after_empty"  # noqa: S105

    @pytest.mark.asyncio
    async def test_sync_error_keeps_watchdog_clock_on_latest_activity(self, bot: AgentBot) -> None:
        """Sync errors should keep the watchdog alive using the latest observed sync activity."""
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(join={})
        sync_error = MagicMock(spec=nio.SyncError)
        bot._first_sync_done = True

        monotonic_values = iter([100.0, 200.0])

        def monotonic_side_effect() -> float:
            return next(monotonic_values, 200.0)

        with patch("mindroom.bot.time.monotonic", side_effect=monotonic_side_effect):
            await bot._on_sync_response(sync_response)
            await bot._on_sync_error(sync_error)

        assert bot._last_sync_monotonic == 200.0

    @pytest.mark.asyncio
    async def test_live_redaction_tombstones_the_source_it_names(self, bot: AgentBot) -> None:
        """The redaction callback owes exactly one thing: the durable tombstone."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_agent:localhost")
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.redacts = "$source:localhost"

        with patch.object(
            bot._turn_store,
            "mark_source_redacted",
        ) as mark_source_redacted:
            await bot._on_redaction(room, redaction_event)

        mark_source_redacted.assert_called_once_with("$source:localhost")

    @pytest.mark.asyncio
    async def test_live_redaction_failure_does_not_rewind_raw_sync_position(
        self,
        bot: AgentBot,
    ) -> None:
        """Durable exact work, not raw token rewind, owns redaction retry."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_agent:localhost")
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.redacts = "$source:localhost"
        _save_certified_sync_token(bot, "s_before_redaction")
        bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_redaction")
        bot.client.next_batch = "s_after_redaction"

        with (
            patch.object(
                bot._turn_store,
                "mark_source_redacted",
                side_effect=RuntimeError("persist failed"),
            ),
            pytest.raises(RuntimeError, match="persist failed"),
        ):
            await bot._on_redaction(room, redaction_event)

        assert bot.client.next_batch == "s_after_redaction"
        assert _load_sync_token_value(bot.storage_path, bot.agent_name) == "s_before_redaction"

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

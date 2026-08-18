"""Bot sync lifecycle: startup cleanup, checkpoint certification, and background drains.

What is left here after the advisory event cache was deleted. Everything this
file used to assert about cached sync timelines went with the writer that
produced them; these tests are about the bot's own lifecycle, which the cache
only ever happened to share a sync callback with.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, current_task_is_process_shutdown
from mindroom.hooks import EVENT_AGENT_STARTED
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN, SYNC_RESTART_SHUTDOWN
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _make_client_mock,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


class TestBotSyncLifecycle(ThreadingBehaviorTestBase):
    """Startup, checkpoint certification, redaction ownership, and drain behavior."""

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
        bot.hook_registry = MagicMock()
        bot.hook_registry.has_hooks.side_effect = lambda event_name: event_name == EVENT_AGENT_STARTED

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch(
                "mindroom.bot.login_agent_owned_session",
                AsyncMock(return_value=SimpleNamespace(client=start_client, session=ingestion_session)),
            ),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
            patch("mindroom.bot.emit", AsyncMock(side_effect=startup_error)),
            pytest.raises(type(startup_error), match="hook boom" if isinstance(startup_error, RuntimeError) else None),
        ):
            await bot.start()

        start_client.close.assert_awaited_once()
        ingestion_session.close.assert_awaited_once()
        assert close_order == ["ingestion", "http"]
        assert bot.running is False
        assert bot.client is None
        assert bot._ingestion_session is None

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

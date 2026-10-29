"""The one owner of scheduled-task runners: which bot runs each task, and when its runner may use that bot.

Each task has one owner bot, decided once when the task is saved and stored
with it, so every bot that restores a room agrees on it. A runner holds no
Matrix client: each Matrix operation borrows the owner's live bot, so a runner
waits through its owner's restarts and never uses a client that a stop closed.
"""

from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import nio

from mindroom.approval import manager as approval_manager
from mindroom.constants import ROUTER_AGENT_NAME, SCHEDULED_TASK_EVENT_TYPE
from mindroom.entity_resolution import entity_identity_registry
from mindroom.failure_text import failure_text
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.message_target import MessageTarget
from mindroom.requester_identity import equivalent_requester_ids
from mindroom.runtime_shutdown import SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS
from mindroom.scheduling import executor as scheduling_executor
from mindroom.scheduling.ownership import room_from_state, task_owner
from mindroom.scheduling.recurring_schedule import (
    RecurringCheckpointUnavailableError,
    complete_recurring_occurrence,
    plan_recurring_occurrence,
)
from mindroom.scheduling.tasks import (
    SCHEDULE_CANCELLED_REASON,
    ScheduledTaskStateReadError,
    get_scheduled_task,
    persist_scheduled_task_state,
    schedule_edit_lock,
    scheduled_call_workflow_digest,
    scheduled_task_timezone,
    task_records_with_writers,
    withdraw_scheduled_call,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ScheduledApprovalArmState
    from mindroom.hooks import HookMatrixAdmin
    from mindroom.scheduling.recurring_schedule import RecurringOccurrence
    from mindroom.scheduling.tasks import ScheduledTaskRecord, ScheduledWorkflow

logger = get_logger(__name__)


# How often a runner checks whether its owner runs again.
_OWNER_POLL_INTERVAL_SECONDS = 1.0
# How often a running task re-reads its persisted state for edits and cancellations.
_TASK_STATE_POLL_INTERVAL_SECONDS = 30
# A missed one-time task older than this is marked failed instead of run when its room is restored.
_MISSED_TASK_MAX_AGE_SECONDS = 86400
# Reason shown on a scheduled tool call's approval card when its trigger never went out.
_SCHEDULE_UNSENT_REASON = "Scheduled task did not run."
_OWNER_REMOVED_REASON = "Schedule failed because its agent was removed."


@dataclass
class ScheduledTaskRuntime:
    """Own every scheduled-task runner in the process and gate each runner's use of its owner bot."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bots: Callable[[], Mapping[str, AgentBot]]
    # The router-backed admin helper that schedule hooks receive.
    matrix_admin: Callable[[], HookMatrixAdmin | None]
    _runners: dict[str, tuple[_TaskRunner, asyncio.Task[None]]] = field(default_factory=dict, init=False)
    # Bots that are stopping; a replacement bot is a new object and is never fenced.
    _fenced: weakref.WeakSet[AgentBot] = field(default_factory=weakref.WeakSet, init=False)
    _in_flight: dict[AgentBot, int] = field(default_factory=dict, init=False)
    _operation_ended: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _stopping: bool = field(default=False, init=False)

    def _start_runner(self, owner: str, task_id: str, workflow: ScheduledWorkflow) -> bool:
        """Start one task's runner under its owner unless a runner for it already runs."""
        if self._stopping:
            return False
        running = self._runners.get(task_id)
        if running is not None and not running[1].done():
            logger.debug("Scheduled task already running; skipping duplicate start", task_id=task_id)
            return False
        if not workflow.room_id:
            logger.error("No room_id provided for scheduled task", task_id=task_id)
            return False
        runner = _TaskRunner(self, owner, workflow.room_id, task_id)
        run = runner.run_once(workflow) if workflow.schedule_type == "once" else runner.run_cron(workflow)
        # A schedule outlives the tool call or command that creates it, and a turn's contextvars hold its Agent and tools.
        self._runners[task_id] = (runner, asyncio.create_task(run, name=f"scheduled_task_{task_id}", context=Context()))
        return True

    def start_new_task(self, task_id: str, workflow: ScheduledWorkflow, *, owner_id: str) -> None:
        """Start a just-saved task's runner under the owner stored with it."""
        owner = entity_identity_registry(self._config(), self.runtime_paths).current_entity_name_for_user_id(owner_id)
        if owner is None:
            logger.error("scheduled_task_owner_unconfigured", task_id=task_id, owner_id=owner_id)
            return
        self._start_runner(owner, task_id, workflow)

    def cancel_runner(self, task_id: str) -> None:
        """Cancel one task's runner if it runs."""
        running = self._runners.pop(task_id, None)
        if running is not None:
            running[1].cancel()

    async def restore_room(self, bot: AgentBot, room_id: str) -> int:
        """Start runners for the pending tasks this bot owns in a room it joined; return how many started.

        A task whose owner is no longer configured is failed by any restoring bot
        that may write the room's schedule state.
        """
        client = bot.client
        if client is None or bot in self._fenced:
            return 0
        try:
            with self._tracked(bot):
                restored = await self._restore_room(bot, client, room_id)
        except Exception:
            logger.exception("scheduled_task_restore_failed", agent=bot.agent_name, room_id=room_id)
            return 0
        if restored > 0:
            logger.info("restored_scheduled_tasks", agent=bot.agent_name, room_id=room_id, restored_task_count=restored)
        return restored

    async def _restore_room(self, bot: AgentBot, client: nio.AsyncClient, room_id: str) -> int:
        response = await client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            logger.warning("scheduled_task_room_state_unread", room_id=room_id, error=str(response))
            return 0
        tasks = task_records_with_writers(room_id, response, self.runtime_paths)
        if not tasks:
            return 0
        config = self._config()
        registry = entity_identity_registry(config, self.runtime_paths)
        router_id = registry.current_id(ROUTER_AGENT_NAME).full_id
        room = room_from_state(room_id, client.user_id, response.events)
        restored = 0
        for task, writer_id in tasks:
            # LEGACY_COMPAT: Scheduled tasks without a stored owner.
            # Legacy format: Scheduled-task state content without an "owner" field; every task saved before owners
            #   were stored.
            # Last legacy release: v2026.10.673 wrote scheduled-task state without an owner; replacement: the next
            #   release stores the owner's Matrix ID in the task's state content when the task is saved.
            # Handling: The owner is decided by the current rule over the room's full state, with the task's latest
            #   state writer standing in for its creator. The field is never written back, so a rewrite keeps the task
            #   on this rule.
            # Coverage: tests/test_scheduled_task_runtime.py::test_task_saved_without_an_owner_restores_under_the_rule_over_its_writer.
            owner_id = task.owner_id or task_owner(room, task.workflow, creator_id=writer_id, router_id=router_id)
            owner = registry.current_entity_name_for_user_id(owner_id)
            if owner is None:
                if not room.power_levels.can_user_send_state(client.user_id, SCHEDULED_TASK_EVENT_TYPE):
                    logger.warning(
                        "scheduled_task_of_removed_owner_skipped",
                        room_id=room_id,
                        task_id=task.task_id,
                        owner_id=owner_id,
                    )
                    continue
                await self._fail_owner_removed_task(bot, room_id, task.task_id, owner_id)
                continue
            if owner == bot.agent_name and await self._restore_task(bot, client, task, config):
                restored += 1
        return restored

    async def _restore_task(
        self,
        bot: AgentBot,
        client: nio.AsyncClient,
        task: ScheduledTaskRecord,
        config: Config,
    ) -> bool:
        workflow = task.workflow
        if workflow.schedule_type == "cron" and not workflow.cron_schedule:
            logger.warning("skipping_recurring_task_without_cron_schedule", task_id=task.task_id)
            return False
        if workflow.schedule_type == "once":
            if not workflow.execute_at:
                logger.warning("skipping_one_time_task_without_execution_time", task_id=task.task_id)
                return False
            missed_by = (datetime.now(UTC) - workflow.execute_at).total_seconds()
            if missed_by > _MISSED_TASK_MAX_AGE_SECONDS:
                logger.warning("Skipping ancient missed task", task_id=task.task_id, missed_by_seconds=missed_by)
                try:
                    await _save_task_status(
                        client,
                        task,
                        "failed",
                        timezone=scheduled_task_timezone(workflow, config),
                        writers=self,
                    )
                except Exception:
                    logger.exception("Failed to mark ancient task as failed", task_id=task.task_id)
                return False
            if missed_by > 0:
                # Its runner fires once the owner finished its first sync.
                logger.warning("Restored missed one-time task", task_id=task.task_id, missed_by_seconds=missed_by)
        return self._start_runner(bot.agent_name, task.task_id, workflow)

    async def fail_removed(self, entity_names: set[str]) -> None:
        """Fail the running tasks of entities removed from the config, while their bots still have their clients.

        A removed entity's tasks are never handed to another bot: each of its
        runners ends, and its task is marked failed while the removed bot says
        so where the task posts. The reload waits here with response admission
        closed, so the tasks settle concurrently within the stop budget; a
        homeserver that stalls past it leaves the rest pending or failed without
        their notice.
        """
        removed = [runner for runner, _task in self._runners.values() if runner.owner in entity_names]
        cancelled = [self._runners.pop(runner.task_id)[1] for runner in removed]
        for task in cancelled:
            task.cancel()
        await asyncio.gather(*cancelled, return_exceptions=True)
        bots = self.bots()
        settling = []
        for runner in removed:
            bot = bots.get(runner.owner)
            if bot is None or bot.client is None:
                logger.warning(
                    "scheduled_task_of_removed_owner_left_pending",
                    owner=runner.owner,
                    task_id=runner.task_id,
                )
                continue
            settling.append(self._fail_owner_removed_task(bot, runner.room_id, runner.task_id, runner.owner))
        try:
            async with asyncio.timeout(SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS):
                await asyncio.gather(*settling)
        except TimeoutError:
            logger.warning(
                "scheduled_tasks_of_removed_owner_unsettled",
                owners=sorted(entity_names),
                timeout_seconds=SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS,
            )

    async def _fail_owner_removed_task(self, bot: AgentBot, room_id: str, task_id: str, owner: str) -> None:
        """Mark one pending task failed because its owner was removed, and say so where it posts; log any failure."""
        client = bot.client
        assert client is not None
        try:
            async with schedule_edit_lock(client, room_id, task_id):
                task = await _get_pending_task_record(client, room_id, task_id, self.runtime_paths)
                if task is None:
                    return
                if task.workflow.pre_approved_call:
                    await withdraw_scheduled_call(task_id, reason=_OWNER_REMOVED_REASON)
                await _save_task_status(
                    client,
                    task,
                    "failed",
                    timezone=scheduled_task_timezone(task.workflow, self._config()),
                    writers=self,
                )
            logger.warning("scheduled_task_failed_owner_removed", room_id=room_id, task_id=task_id, owner=owner)
            await scheduling_executor.send_scheduled_failure_notice(
                client,
                task.workflow,
                MessageTarget.for_scheduled_task(task.workflow),
                f"❌ Schedule failed because its agent was removed: {task.workflow.description}\nTask ID: `{task_id}`",
                bot._conversation_reader,
            )
        except Exception:
            logger.exception("scheduled_task_owner_removal_failure_failed", task_id=task_id, owner=owner)

    async def release(self, entity_names: set[str]) -> None:
        """Fence entities about to stop, then wait, bounded, for their in-flight Matrix operations.

        Runners are never cancelled for a bot stop; they wait for the owner to
        run again. An operation still running when the bound expires has its
        client closed under it.
        """
        bots = [bot for name, bot in self.bots().items() if name in entity_names]
        self._fenced.update(bots)
        try:
            async with asyncio.timeout(SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS):
                while True:
                    operation_ended = self._operation_ended
                    if not any(bot in self._in_flight for bot in bots):
                        return
                    await operation_ended.wait()
        except TimeoutError:
            logger.warning(
                "scheduled_task_operations_outlived_owner_stop",
                entities=sorted(bot.agent_name for bot in bots if bot in self._in_flight),
            )

    def _is_fenced(self, bot: AgentBot) -> bool:
        """Return whether this bot is stopping, so its operations may have lost their client."""
        return bot in self._fenced

    async def stop(self) -> None:
        """Cancel every runner before process shutdown stops the bots."""
        self._stopping = True
        cancelled = [task for _runner, task in self._runners.values()]
        self._runners.clear()
        for task in cancelled:
            task.cancel()
        await asyncio.gather(*cancelled, return_exceptions=True)

    @asynccontextmanager
    async def operation(self, entity_name: str) -> AsyncIterator[tuple[AgentBot, nio.AsyncClient]]:
        """Use one entity's bot and client for Matrix work once it runs, has synced, and is not stopping."""
        while True:
            bot = self._ready_bot(entity_name)
            if bot is not None:
                break
            await asyncio.sleep(_OWNER_POLL_INTERVAL_SECONDS)
        client = bot.client
        assert client is not None
        with self._tracked(bot):
            yield bot, client

    @contextmanager
    def other_state_writer(self, room_id: str, *, refused_user_id: str) -> Iterator[nio.AsyncClient | None]:
        """Lend a ready bot, other than the refused account, whose power in the room lets it write schedule state."""
        for name in self.bots():
            bot = self._ready_bot(name)
            client = None if bot is None else bot.client
            room = None if client is None else client.rooms.get(room_id)
            if (
                bot is not None
                and client is not None
                and room is not None
                and client.user_id != refused_user_id
                and room.power_levels.can_user_send_state(client.user_id, SCHEDULED_TASK_EVENT_TYPE)
            ):
                with self._tracked(bot):
                    yield client
                return
        yield None

    def router_client(self) -> nio.AsyncClient | None:
        """Return the router's client, the account a room admin should empower for schedule state."""
        router = self.bots().get(ROUTER_AGENT_NAME)
        return None if router is None else router.client

    def _ready_bot(self, entity_name: str) -> AgentBot | None:
        bot = self.bots().get(entity_name)
        if bot is None or bot in self._fenced or not bot.running or not bot.first_sync_complete or bot.client is None:
            return None
        return bot

    @contextmanager
    def _tracked(self, bot: AgentBot) -> Iterator[None]:
        self._in_flight[bot] = self._in_flight.get(bot, 0) + 1
        try:
            yield
        finally:
            remaining = self._in_flight.pop(bot) - 1
            if remaining:
                self._in_flight[bot] = remaining
            operation_ended, self._operation_ended = self._operation_ended, asyncio.Event()
            operation_ended.set()

    def _runner_finished(self, task_id: str) -> None:
        running = self._runners.get(task_id)
        if running is not None and running[1] is asyncio.current_task():
            del self._runners[task_id]

    def _config(self) -> Config:
        config = self.config_provider()
        if config is None:
            msg = "Scheduled tasks need a loaded config"
            raise RuntimeError(msg)
        return config


@dataclass(frozen=True)
class _TaskRunner:
    """Run one scheduled task, borrowing its owner's live bot for each Matrix operation."""

    runtime: ScheduledTaskRuntime
    owner: str
    room_id: str
    task_id: str

    async def _runnable_task(self) -> ScheduledTaskRecord | None:  # noqa: C901
        """Return runnable state, cancelling departed creators' tasks and retrying uncertainty."""
        runtime = self.runtime
        departed_task: ScheduledTaskRecord | None = None
        task: ScheduledTaskRecord | None = None
        while True:
            try:
                async with runtime.operation(self.owner) as (_bot, client):
                    task = await _get_pending_task_record(
                        client=client,
                        room_id=self.room_id,
                        task_id=self.task_id,
                        runtime_paths=runtime.runtime_paths,
                    )
                    if task is None:
                        return None
                    if departed_task != task:
                        membership_config = runtime._config()
                        creator_joined = await _scheduled_task_creator_is_joined(
                            client,
                            task,
                            membership_config,
                            runtime.runtime_paths,
                        )
                        if runtime._config() is not membership_config:
                            continue
                        if creator_joined:
                            return task
                        departed_task = task

                    async with schedule_edit_lock(client, self.room_id, self.task_id):
                        current_task = await _get_pending_task_record(
                            client=client,
                            room_id=self.room_id,
                            task_id=self.task_id,
                            runtime_paths=runtime.runtime_paths,
                        )
                        if current_task is None:
                            return None
                        if current_task != departed_task:
                            continue
                        if current_task.workflow.pre_approved_call:
                            await withdraw_scheduled_call(
                                self.task_id,
                                reason=SCHEDULE_CANCELLED_REASON,
                            )
                        await _save_task_status(
                            client,
                            current_task,
                            "cancelled",
                            timezone=scheduled_task_timezone(current_task.workflow, runtime._config()),
                            writers=runtime,
                        )
            except ScheduledTaskStateReadError as exc:
                logger.warning(
                    "scheduled_task_state_read_failed_retrying",
                    room_id=self.room_id,
                    task_id=self.task_id,
                    error=str(exc),
                )
                await asyncio.sleep(_TASK_STATE_POLL_INTERVAL_SECONDS)
            except ValueError as exc:
                logger.warning(
                    "scheduled_task_owner_cancellation_failed_retrying",
                    room_id=self.room_id,
                    task_id=self.task_id,
                    error=str(exc),
                )
                await asyncio.sleep(_TASK_STATE_POLL_INTERVAL_SECONDS)
            else:
                assert task is not None
                logger.info(
                    "scheduled_task_cancelled_after_creator_left",
                    room_id=self.room_id,
                    task_id=self.task_id,
                    created_by=task.workflow.created_by,
                )
                return None

    async def _checkpoint_identity(self) -> tuple[str, str]:
        """Return the homeserver and account that key the owner's recurring checkpoint."""
        async with self.runtime.operation(self.owner) as (_bot, client):
            return client.homeserver, client.user_id

    async def _plan_occurrence(self, workflow: ScheduledWorkflow, cron: str) -> RecurringOccurrence:
        homeserver, sender = await self._checkpoint_identity()
        # Sampled once the owner is ready, so an owner restart's wait counts against the catch-up window. Cron has
        # second precision; normal subsecond wake-up jitter must still fire a live timer when catch-up is disabled.
        now = datetime.now(UTC).replace(microsecond=0)
        return await plan_recurring_occurrence(
            self.runtime.runtime_paths,
            homeserver=homeserver,
            sender=sender,
            room_id=self.room_id,
            task_id=self.task_id,
            workflow_json=workflow.model_dump_json(),
            cron=cron,
            now=now,
            grace_seconds=self.runtime._config().scheduler_catch_up_grace_seconds,
        )

    async def _execute(
        self,
        workflow: ScheduledWorkflow,
        occurrence: RecurringOccurrence | None = None,
    ) -> scheduling_executor.ScheduledWorkflowOutcome:
        async with self.runtime.operation(self.owner) as (bot, client):
            return await scheduling_executor.execute_scheduled_workflow(
                client,
                workflow,
                self.runtime._config(),
                self.runtime.runtime_paths,
                bot._conversation_reader,
                self.task_id,
                self.runtime.matrix_admin(),
                occurrence=occurrence,
            )

    async def _send_failure_notice(self, workflow: ScheduledWorkflow, target: MessageTarget, message: str) -> None:
        async with self.runtime.operation(self.owner) as (bot, client):
            await scheduling_executor.send_scheduled_failure_notice(
                client,
                workflow,
                target,
                message,
                bot._conversation_reader,
            )

    async def _save_status(self, task: ScheduledTaskRecord, status: str) -> None:
        """Persist a one-time task's final status; a write cut off by its owner's stop is retried once it runs again."""
        while True:
            async with self.runtime.operation(self.owner) as (bot, client):
                try:
                    await _save_task_status(
                        client,
                        task,
                        status,
                        timezone=scheduled_task_timezone(task.workflow, self.runtime._config()),
                        writers=self.runtime,
                    )
                except Exception:
                    if not self.runtime._is_fenced(bot):
                        logger.exception(
                            "Failed to persist one-time task final state",
                            task_id=self.task_id,
                            status=status,
                        )
                        return
                else:
                    return
            logger.warning("scheduled_task_status_write_interrupted_by_owner_stop", task_id=self.task_id, status=status)

    async def run_cron(self, workflow: ScheduledWorkflow) -> None:
        """Run a recurring task based on its cron schedule.

        Each turn reads the task's current state and plans from it, so an edit
        replans from the edited workflow and a cancellation ends the runner.
        """
        task_id = self.task_id
        current_target = MessageTarget.for_scheduled_task(workflow)
        try:
            while True:
                latest_task = await self._runnable_task()
                if not latest_task:
                    with bound_log_context(**current_target.log_context):
                        logger.info("Recurring task is no longer pending, stopping", task_id=task_id)
                    return

                workflow = latest_task.workflow
                current_target = MessageTarget.for_scheduled_task(workflow)
                with bound_log_context(**current_target.log_context):
                    cron_schedule = workflow.cron_schedule
                    if not cron_schedule:
                        logger.error("No cron schedule provided for recurring task", task_id=task_id)
                        return
                    cron_string = cron_schedule.to_cron_string()
                    try:
                        occurrence = await self._plan_occurrence(workflow, cron_string)
                    except RecurringCheckpointUnavailableError:
                        await asyncio.sleep(_TASK_STATE_POLL_INTERVAL_SECONDS)
                        continue
                    delay = (occurrence.checkpoint.next_run_at - datetime.now(UTC)).total_seconds()
                    if delay > 0:
                        await asyncio.sleep(min(delay, _TASK_STATE_POLL_INTERVAL_SECONDS))
                        continue

                    outcome = await self._execute(workflow, occurrence)
                    if outcome.status in {"retry", "held"}:
                        await asyncio.sleep(_TASK_STATE_POLL_INTERVAL_SECONDS)
                        continue
                    await complete_recurring_occurrence(occurrence, cron_string, datetime.now(UTC))
        except asyncio.CancelledError:
            with bound_log_context(**current_target.log_context):
                logger.info("cron_task_cancelled", task_id=task_id)
            raise
        except Exception as e:
            with bound_log_context(**current_target.log_context):
                logger.exception("cron_task_failed", task_id=task_id)
                if workflow.room_id:
                    error_message = f"❌ Recurring task failed: {workflow.description}\nTask ID: {task_id}\nError: {failure_text(e)}"
                    await self._send_failure_notice(workflow, current_target, error_message)
        finally:
            self.runtime._runner_finished(task_id)

    async def run_once(self, workflow: ScheduledWorkflow) -> None:  # noqa: C901, PLR0912, PLR0915
        """Run a one-time scheduled task."""
        task_id = self.task_id
        current_target = MessageTarget.for_scheduled_task(workflow)
        latest_pending_task: ScheduledTaskRecord | None = None
        approval_state: ScheduledApprovalArmState = "none"
        try:
            while True:
                latest_task = await self._runnable_task()
                if not latest_task:
                    with bound_log_context(**current_target.log_context):
                        logger.info("One-time task is no longer pending, stopping", task_id=task_id)
                    return

                workflow = latest_task.workflow
                current_target = MessageTarget.for_scheduled_task(workflow)
                with bound_log_context(**current_target.log_context):
                    execute_at = workflow.execute_at
                    if not execute_at:
                        logger.error("No execution time provided for one-time task", task_id=task_id)
                        return

                    delay = (execute_at - datetime.now(UTC)).total_seconds()
                    if delay <= 0:
                        break
                    await asyncio.sleep(min(delay, _TASK_STATE_POLL_INTERVAL_SECONDS))

            latest_pending_task = latest_task
            with bound_log_context(**current_target.log_context):
                approval_state = (
                    await _arm_scheduled_call(task_id, workflow, self.runtime._config())
                    if workflow.pre_approved_call
                    else "none"
                )
                if approval_state == "denied":
                    logger.info("scheduled_tool_call_skipped_after_denial", task_id=task_id)
                    final_status = "cancelled"
                else:
                    outcome = await self._execute(workflow)
                    if approval_state == "armed" and outcome.status != "delivered":
                        await _withdraw_unsent_scheduled_call(task_id)
                    final_status = "completed" if outcome.status == "delivered" else "failed"

                # After a send, only the status write is retried; the send never repeats.
                await self._save_status(latest_pending_task, final_status)
        except asyncio.CancelledError:
            with bound_log_context(**current_target.log_context):
                logger.info("one_time_task_cancelled", task_id=task_id)
            raise
        except Exception as e:
            with bound_log_context(**current_target.log_context):
                logger.exception("one_time_task_failed", task_id=task_id)
                if approval_state == "armed":
                    await _withdraw_unsent_scheduled_call(task_id)
                if workflow.room_id:
                    error_message = (
                        f"❌ One-time task failed: {workflow.description}\nTask ID: {task_id}\nError: {failure_text(e)}"
                    )
                    await self._send_failure_notice(workflow, current_target, error_message)
                if latest_pending_task is not None:
                    await self._save_status(latest_pending_task, "failed")
        finally:
            self.runtime._runner_finished(task_id)


async def _get_pending_task_record(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    runtime_paths: RuntimePaths,
) -> ScheduledTaskRecord | None:
    """Return the latest pending task state for a task id, if it still exists."""
    task_record = await get_scheduled_task(
        client=client,
        room_id=room_id,
        task_id=task_id,
        runtime_paths=runtime_paths,
    )
    if not task_record or task_record.status != "pending":
        return None
    return task_record


async def _scheduled_task_creator_is_joined(
    client: nio.AsyncClient,
    task: ScheduledTaskRecord,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Check live membership for the creator or a permitted human alias."""
    creator = task.workflow.created_by
    assert creator is not None
    requester_ids = equivalent_requester_ids(creator, config, runtime_paths)
    membership_unknown = False
    for requester_id in sorted(requester_ids, key=lambda user_id: (user_id != creator, user_id)):
        try:
            response = await client.room_get_state_event(
                room_id=task.room_id,
                event_type="m.room.member",
                state_key=requester_id,
            )
        except Exception:
            membership_unknown = True
            continue
        if isinstance(response, nio.RoomGetStateEventResponse) and isinstance(response.content, dict):
            membership = response.content.get("membership")
            if membership == "join":
                return True
            if membership in ("leave", "ban", "invite", "knock"):
                continue
        # Errors (including missing/inaccessible state) do not prove a departure.
        membership_unknown = True
    if membership_unknown:
        msg = f"Could not establish schedule creator membership for {creator!r} in room {task.room_id!r}"
        raise ScheduledTaskStateReadError(msg)
    return False


async def _save_task_status(
    client: nio.AsyncClient,
    task: ScheduledTaskRecord,
    status: str,
    *,
    timezone: str,
    writers: ScheduledTaskRuntime,
) -> None:
    """Persist a task's new status, keeping its stored owner, without touching its runner."""
    await persist_scheduled_task_state(
        client=client,
        room_id=task.room_id,
        task_id=task.task_id,
        workflow=task.workflow,
        timezone=timezone,
        owner_id=task.owner_id,
        status=status,
        created_at=task.created_at,
        writers=writers,
    )


async def _withdraw_unsent_scheduled_call(task_id: str) -> None:
    """Withdraw an armed approval whose trigger never went out, so no other call can use it."""
    try:
        await withdraw_scheduled_call(task_id, reason=_SCHEDULE_UNSENT_REASON)
    except ValueError:
        logger.exception("scheduled_tool_call_withdrawal_failed", task_id=task_id)


async def _arm_scheduled_call(
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
) -> ScheduledApprovalArmState:
    """Arm a firing task's call approval; an approval-journal error leaves the call asking at send time."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return "none"
    try:
        return await manager.arm_scheduled_call_approval(
            task_id,
            scheduled_call_workflow_digest(task_id, workflow),
            any_arguments_allowed=config.tool_approval.scheduled_any_arguments,
        )
    except Exception:
        logger.warning("scheduled_tool_call_arming_failed", task_id=task_id, exc_info=True)
        return "unarmed"

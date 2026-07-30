"""Debounced config reload and shared response-admission replacement lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.config.main import load_config
from mindroom.config.yaml_includes import partial_source_files
from mindroom.logging_config import get_logger
from mindroom.orchestration.config_updates import (
    build_config_update_plan,
    configured_entity_names,
    plugin_change_paths,
)
from mindroom.orchestration.runtime import cancel_logged_task, create_logged_task

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.orchestration.config_updates import ConfigUpdatePlan
    from mindroom.response_admission import ResponseAdmissionGate

logger = get_logger(__name__)


_CONFIG_RELOAD_DEBOUNCE_SECONDS = 2.0
_REPLACEMENT_DRAIN_IDLE_POLL_SECONDS = 0.5
_REPLACEMENT_DRAIN_WARNING_AFTER_SECONDS = 30.0
_REPLACEMENT_DRAIN_WARNING_INTERVAL_SECONDS = 30.0
# The in-flight count includes responses still queued behind a conversation lock,
# so a busy install may never observe a fully idle moment. Bound the wait rather
# than letting a replacement be deferred forever.
_REPLACEMENT_DRAIN_FORCE_AFTER_SECONDS = 600.0


@dataclass
class _ReplacementDrainState:
    """Track response-drain state for one replacement apply."""

    waiting_for_idle: bool = False
    wait_started_at: float | None = None
    last_warning_at: float | None = None

    def begin_wait(self, *, now: float) -> None:
        """Start a fresh response-drain window."""
        self.waiting_for_idle = True
        self.wait_started_at = now
        self.last_warning_at = None

    def wait_seconds(self, now: float) -> float:
        """Return how long the current drain window has been waiting."""
        if self.wait_started_at is None:
            return 0.0
        return now - self.wait_started_at

    def should_warn(
        self,
        *,
        now: float,
        warning_after_seconds: float,
        warning_interval_seconds: float,
    ) -> bool:
        """Return whether the current drain should emit a warning."""
        if self.wait_started_at is None or self.wait_seconds(now) < warning_after_seconds:
            return False
        if self.last_warning_at is None:
            return True
        return now - self.last_warning_at >= warning_interval_seconds

    def mark_warning(self, now: float) -> None:
        """Record the time a drain warning was logged."""
        self.last_warning_at = now

    def should_force_apply(self, *, now: float, force_after_seconds: float) -> bool:
        """Return whether the drain has waited long enough to stop deferring."""
        return self.wait_started_at is not None and self.wait_seconds(now) >= force_after_seconds


@dataclass
class ConfigReloadLifecycle:
    """Own debounced config reloads and serialized replacement admission.

    The orchestrator stays the owner of applying a plan (restarting bots,
    reconciling accounts and rooms). This collaborator owns when a config
    reload runs, how it is diffed into a plan, and the global admission window
    shared by config reloads and asynchronous MCP catalog replacements.
    """

    runtime_paths: RuntimePaths
    is_running: Callable[[], bool]
    current_config: Callable[[], Config | None]
    agent_bots: Callable[[], Mapping[str, AgentBot | TeamBot]]
    load_initial_config: Callable[[Config], Awaitable[bool]]
    apply_update_plan: Callable[[Config, ConfigUpdatePlan, tuple[str, ...]], Awaitable[bool]]
    response_admission_gate: ResponseAdmissionGate
    # Shared with manual plugin reloads and MCP catalog-change handling so no
    # two publication flows can interleave their read-plan-apply sequences.
    config_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _response_admission_apply_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _reload_task: asyncio.Task | None = field(default=None, init=False)
    _requested_at: float | None = field(default=None, init=False)
    # Source files of the last reload attempt that failed to load, so the
    # config watcher can cover include files the last good config never saw.
    failed_reload_source_files: frozenset[Path] | None = field(default=None, init=False)

    def request_reload(self) -> None:
        """Queue a debounced config reload for the running orchestrator."""
        if not self.is_running():
            logger.info("Ignoring config change while startup is still in progress")
            return
        self._requested_at = asyncio.get_running_loop().time()
        if self._reload_task is not None and not self._reload_task.done():
            logger.info("Configuration reload already queued; extending debounce window")
            return
        logger.info("Queued configuration reload")
        self._reload_task = create_logged_task(
            self._run_reload_loop(),
            name="config_reload",
            failure_message="Queued config reload failed",
        )

    async def cancel(self) -> None:
        """Cancel any queued config reload task."""
        task = self._reload_task
        self._reload_task = None
        self._requested_at = None
        await cancel_logged_task(task)

    async def update_config(self) -> bool:
        """Reload configuration from disk and dispatch the resulting update plan."""
        async with self.config_update_lock:
            # Config validation executes plugin modules and walks the filesystem;
            # keep it off the event loop (#1260).
            new_config = await asyncio.to_thread(load_config, self.runtime_paths, tolerate_plugin_load_errors=True)
            current_config = self.current_config()
            if current_config is None:
                return await self.load_initial_config(new_config)

            agent_bots = self.agent_bots()
            plugin_changes = plugin_change_paths(current_config, new_config)
            plan = build_config_update_plan(
                current_config=current_config,
                new_config=new_config,
                configured_entities=set(configured_entity_names(new_config)),
                existing_entities=set(agent_bots.keys()),
                agent_bots=agent_bots,
            )
            if plugin_changes:
                plan = replace(plan, entities_to_restart=plan.entities_to_restart | set(agent_bots))
            return await self.apply_update_plan(current_config, plan, plugin_changes)

    async def _wait_for_reload_debounce(
        self,
        requested_at: float,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Sleep until the debounce window closes for a queued reload request."""
        reload_at = requested_at + _CONFIG_RELOAD_DEBOUNCE_SECONDS
        delay_seconds = reload_at - loop.time()
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    async def _should_defer_replacement_for_active_responses(
        self,
        *,
        drain_state: _ReplacementDrainState,
        active_response_count: int,
        loop: asyncio.AbstractEventLoop,
        operation_name: str,
    ) -> bool:
        """Return whether one replacement apply should keep waiting for responses.

        Only called with responses actually in flight: the caller applies
        immediately when ``close_if_idle()`` succeeds.
        """
        now = loop.time()
        if not drain_state.waiting_for_idle:
            logger.info(
                "Deferring replacement until active responses finish",
                operation=operation_name,
                active_response_count=active_response_count,
            )
            drain_state.begin_wait(now=now)
        elif drain_state.should_warn(
            now=now,
            warning_after_seconds=_REPLACEMENT_DRAIN_WARNING_AFTER_SECONDS,
            warning_interval_seconds=_REPLACEMENT_DRAIN_WARNING_INTERVAL_SECONDS,
        ):
            logger.warning(
                "Replacement still waiting for active responses to finish",
                operation=operation_name,
                active_response_count=active_response_count,
                drain_wait_seconds=round(drain_state.wait_seconds(now), 1),
            )
            drain_state.mark_warning(now)

        if drain_state.should_force_apply(
            now=now,
            force_after_seconds=_REPLACEMENT_DRAIN_FORCE_AFTER_SECONDS,
        ):
            logger.error(
                "Applying replacement while responses are still active",
                operation=operation_name,
                active_response_count=active_response_count,
                drain_wait_seconds=round(drain_state.wait_seconds(now), 1),
                timeout_seconds=_REPLACEMENT_DRAIN_FORCE_AFTER_SECONDS,
            )
            return False

        await asyncio.sleep(_REPLACEMENT_DRAIN_IDLE_POLL_SECONDS)
        return True

    async def _apply_with_closed_admission(
        self,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        """Apply one replacement with admission already closed, then always reopen it.

        Callers must close the gate immediately before calling, with no await in
        between, so nothing can be admitted into the window.

        The gate is closed but not held for the duration: applying the plan stops
        bots, and stopping a bot drains its detached responses, so holding the
        gate here would stall that drain until it cancelled the very responses
        this flow exists to protect.
        """
        assert self.response_admission_gate.closed, "admission must be closed before applying"
        try:
            await operation()
        finally:
            self.response_admission_gate.reopen()

    async def apply_with_response_admission(
        self,
        operation: Callable[[], Awaitable[None]],
        *,
        operation_name: str,
        request_is_current: Callable[[], bool],
    ) -> None:
        """Run one global serialized replacement after a bounded response drain.

        Config reloads call this from their debounced task. MCP catalog changes
        call it from an orchestrator-owned background task so an admitted tool
        call cannot wait on its own slot. The drain defers for at most 600
        seconds before closing admission over a forced apply.
        """
        loop = asyncio.get_running_loop()
        drain_state = _ReplacementDrainState()
        async with self._response_admission_apply_lock:
            while request_is_current():
                if self.response_admission_gate.close_if_idle():
                    if drain_state.waiting_for_idle:
                        logger.info(
                            "Active responses finished; applying replacement",
                            operation=operation_name,
                        )
                    break
                if await self._should_defer_replacement_for_active_responses(
                    drain_state=drain_state,
                    active_response_count=self.response_admission_gate.in_flight_response_count,
                    loop=loop,
                    operation_name=operation_name,
                ):
                    continue
                self.response_admission_gate.close()
                break
            else:
                return

            await self._apply_with_closed_admission(operation)

    async def _apply_queued_config_reload(self) -> None:
        """Apply one queued config reload attempt and log the result."""
        self._requested_at = None
        logger.info("Configuration file changed, checking for updates...")
        try:
            updated = await self.update_config()
        except Exception as exc:
            logger.exception("Configuration update failed; will retry if a new change is queued")
            # Keep watching every file the broken load read so fixing a newly
            # added include file (not yet in the last good config) still
            # triggers the retry reload.
            failed_files = partial_source_files(exc)
            if failed_files is not None:
                self.failed_reload_source_files = failed_files
            return
        self.failed_reload_source_files = None
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")

    async def _run_reload_loop(self) -> None:
        """Apply queued config reloads after debounce and response drain."""
        current_task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        try:
            while self.is_running() and self._requested_at is not None:
                requested_at = self._requested_at
                await self._wait_for_reload_debounce(requested_at, loop)
                if self._requested_at != requested_at:
                    # A newer config change superseded the current one.
                    continue

                await self.apply_with_response_admission(
                    self._apply_queued_config_reload,
                    operation_name="configuration reload",
                    request_is_current=lambda requested_at=requested_at: self._requested_at == requested_at,
                )
        finally:
            if self._reload_task is current_task:
                self._reload_task = None

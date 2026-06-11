"""Debounced config-reload lifecycle for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.config.main import load_config
from mindroom.logging_config import get_logger
from mindroom.orchestration.config_updates import (
    build_config_update_plan,
    configured_entity_names,
    plugin_change_paths,
)
from mindroom.orchestration.runtime import cancel_logged_task, create_logged_task

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.orchestration.config_updates import ConfigUpdatePlan

logger = get_logger(__name__)

_CONFIG_RELOAD_DEBOUNCE_SECONDS = 2.0
_CONFIG_RELOAD_IDLE_POLL_SECONDS = 0.5
_CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS = 30.0
_CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS = 30.0
_CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS = 120.0


@dataclass
class _ConfigReloadDrainState:
    """Track response-drain state for a queued config reload."""

    waiting_for_idle: bool = False
    wait_started_at: float | None = None
    last_warning_at: float | None = None
    request_started_at: float | None = None

    def reset(self) -> None:
        """Clear all drain tracking state."""
        self.waiting_for_idle = False
        self.wait_started_at = None
        self.last_warning_at = None
        self.request_started_at = None

    def begin_wait(self, *, now: float, requested_at: float) -> None:
        """Start a fresh drain window for the current reload request."""
        self.waiting_for_idle = True
        self.wait_started_at = now
        self.last_warning_at = None
        self.request_started_at = requested_at

    def should_reset_for_request(self, requested_at: float) -> bool:
        """Return whether a newer request should restart the drain window."""
        return self.waiting_for_idle and self.request_started_at != requested_at

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

    def should_force_reload(self, *, now: float, force_after_seconds: float) -> bool:
        """Return whether the drain timeout has expired."""
        return self.wait_started_at is not None and self.wait_seconds(now) >= force_after_seconds


@dataclass
class ConfigReloadLifecycle:
    """Own debounced config reloads: queueing, response drain, and plan dispatch.

    The orchestrator stays the owner of applying a plan (restarting bots,
    reconciling accounts and rooms); this collaborator owns when a reload
    runs and how the new config is diffed into a plan.
    """

    runtime_paths: RuntimePaths
    is_running: Callable[[], bool]
    current_config: Callable[[], Config | None]
    agent_bots: Callable[[], Mapping[str, AgentBot | TeamBot]]
    in_flight_response_count: Callable[[], int]
    load_initial_config: Callable[[Config], Awaitable[bool]]
    apply_update_plan: Callable[[Config, ConfigUpdatePlan, tuple[str, ...]], Awaitable[bool]]
    _reload_task: asyncio.Task | None = field(default=None, init=False)
    _requested_at: float | None = field(default=None, init=False)

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
        new_config = load_config(self.runtime_paths, tolerate_plugin_load_errors=True)
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

    async def _should_defer_reload_for_active_responses(
        self,
        *,
        drain_state: _ConfigReloadDrainState,
        requested_at: float,
        active_response_count: int,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Return whether a queued reload should keep waiting for responses to finish."""
        if active_response_count <= 0:
            return False

        now = loop.time()
        if not drain_state.waiting_for_idle:
            logger.info(
                "Deferring configuration reload until active responses finish",
                active_response_count=active_response_count,
            )
            drain_state.begin_wait(now=now, requested_at=requested_at)
        elif drain_state.should_warn(
            now=now,
            warning_after_seconds=_CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS,
            warning_interval_seconds=_CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS,
        ):
            logger.warning(
                "Configuration reload still waiting for active responses to finish",
                active_response_count=active_response_count,
                drain_wait_seconds=round(drain_state.wait_seconds(now), 1),
            )
            drain_state.mark_warning(now)

        if drain_state.should_force_reload(
            now=now,
            force_after_seconds=_CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS,
        ):
            logger.error(
                "Forcing configuration reload while responses are still active",
                active_response_count=active_response_count,
                drain_wait_seconds=round(drain_state.wait_seconds(now), 1),
                timeout_seconds=_CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS,
            )
            return False

        await asyncio.sleep(_CONFIG_RELOAD_IDLE_POLL_SECONDS)
        return True

    async def _apply_queued_config_reload(self) -> None:
        """Apply one queued config reload attempt and log the result."""
        self._requested_at = None
        logger.info("Configuration file changed, checking for updates...")
        try:
            updated = await self.update_config()
        except Exception:
            logger.exception("Configuration update failed; will retry if a new change is queued")
            return
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")

    async def _run_reload_loop(self) -> None:
        """Apply queued config reloads after debounce and response drain."""
        current_task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        drain_state = _ConfigReloadDrainState()

        try:
            while self.is_running() and self._requested_at is not None:
                requested_at = self._requested_at
                await self._wait_for_reload_debounce(requested_at, loop)
                if self._requested_at != requested_at:
                    # A newer config change superseded the current one.
                    # Reset drain state so the new change gets a full drain window.
                    drain_state.reset()
                    continue

                if drain_state.should_reset_for_request(requested_at):
                    # A newer config change arrived while we were already waiting
                    # for responses to drain, so restart the drain window.
                    drain_state.reset()
                    continue

                active_response_count = self.in_flight_response_count()
                if await self._should_defer_reload_for_active_responses(
                    drain_state=drain_state,
                    requested_at=requested_at,
                    active_response_count=active_response_count,
                    loop=loop,
                ):
                    continue

                if drain_state.waiting_for_idle and active_response_count == 0:
                    logger.info("Active responses finished; applying queued configuration reload")
                if drain_state.waiting_for_idle:
                    drain_state.reset()

                await self._apply_queued_config_reload()
        finally:
            if self._reload_task is current_task:
                self._reload_task = None

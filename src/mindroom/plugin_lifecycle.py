"""Plugin lifespans: the start and stop code of each plugin the primary runtime publishes."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.plugin_context import PluginContext, build_plugin_context
from mindroom.plugin_worker_services import stop_started_worker_services
from mindroom.tool_system.plugins import PluginGeneration, config_plugins, plugin_process_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from contextlib import AbstractAsyncContextManager

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_sources import EventSource, EventSourceEmitter, EventSourceRegistry
    from mindroom.tool_system.plugins import PluginRoutesApp

logger = get_logger(__name__)

_StopReason = Literal["reload", "disabled", "shutdown"]
# A lifecycle module's ``lifespan``.
type PluginLifespan = Callable[[PluginLifecycle], AbstractAsyncContextManager[object]]

# How long a lifespan's start or stop code, or closing a replaced plugin's websockets, may take before MindRoom moves on.
_LIFESPAN_DEADLINE_SECONDS = 10.0


@dataclass(eq=False)
class PluginLifecycle:
    """What a plugin's ``lifespan`` gets: its context, state that outlives reloads, and why it stops."""

    plugin_name: str
    # One dict per plugin name for the process's lifetime, so a resource a lifespan keeps here outlives a reload.
    process_state: dict[str, Any]
    _runtime_paths: RuntimePaths = field(repr=False)
    _current_config: Callable[[], Config] = field(repr=False)
    _event_sources: EventSourceRegistry | None = field(default=None, repr=False)
    # Set before the stop code runs: a newer lifespan of the plugin started and can take over ``process_state``
    # (``reload``), none did (``disabled``), or MindRoom stops (``shutdown``).
    stop_reason: _StopReason | None = field(default=None, init=False)
    _registered_event_sources: list[EventSourceEmitter] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def context(self) -> PluginContext:
        """Return the plugin's context under the config MindRoom serves now."""
        return build_plugin_context(
            self.plugin_name,
            self._current_config(),
            self._runtime_paths,
            self._current_config,
        )

    def register_event_source(self, source: EventSource) -> EventSourceEmitter:
        """Register an EventSource owned by this plugin for the duration of this lifespan."""
        if self._event_sources is None:
            msg = "Event sources are only available in the primary runtime"
            raise RuntimeError(msg)
        if self._closed or self.stop_reason is not None:
            msg = f"Plugin {self.plugin_name!r} lifespan has ended"
            raise RuntimeError(msg)
        emitter = self._event_sources.register(source, owner_plugin=self.plugin_name)
        self._registered_event_sources.append(emitter)
        return emitter

    def _unregister_event_sources(self) -> None:
        """Unregister every EventSource token recorded on this lifespan, leaving newer reload tokens intact."""
        self._closed = True
        if self._event_sources is None:
            self._registered_event_sources.clear()
            return
        registered, self._registered_event_sources = self._registered_event_sources, []
        for emitter in reversed(registered):
            self._event_sources.unregister(emitter.source_id, emitter.token)


@dataclass(eq=False)
class _RunningLifespan:
    lifecycle: PluginLifecycle
    # Runs the whole lifespan, so its start and stop code share one task, as a task group across its ``yield`` needs.
    task: asyncio.Task[None]
    stopping: asyncio.Event


async def _run(
    lifespan: PluginLifespan,
    lifecycle: PluginLifecycle,
    started: asyncio.Future[None],
    stopping: asyncio.Event,
) -> None:
    try:
        async with lifespan(lifecycle):
            started.set_result(None)
            await stopping.wait()
    finally:
        lifecycle._unregister_event_sources()


async def _start(lifespan: PluginLifespan, lifecycle: PluginLifecycle) -> _RunningLifespan | None:
    """Run ``lifespan`` up to its ``yield``, or log why it did not get there and return None."""
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    stopping = asyncio.Event()
    # A context of its own, so the reload's turn, and its log fields, do not outlive it in the plugin's tasks.
    task = asyncio.create_task(
        _run(lifespan, lifecycle, started, stopping),
        name=f"plugin_lifespan:{lifecycle.plugin_name}",
        context=contextvars.Context(),
    )
    await asyncio.wait({started, task}, timeout=_LIFESPAN_DEADLINE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
    if started.done():
        return _RunningLifespan(lifecycle, task, stopping)
    _log_failure("Plugin lifespan failed to start", lifecycle.plugin_name, task)
    lifecycle._unregister_event_sources()
    return None


async def _stop(running: _RunningLifespan, reason: _StopReason) -> None:
    """Run the lifespan's code after its ``yield`` with ``reason``, logging a stop that raises or overruns."""
    running.lifecycle.stop_reason = reason
    running.stopping.set()
    try:
        await asyncio.wait({running.task}, timeout=_LIFESPAN_DEADLINE_SECONDS)
        _log_failure("Plugin lifespan failed to stop", running.lifecycle.plugin_name, running.task, stop_reason=reason)
    finally:
        running.lifecycle._unregister_event_sources()


def _log_failure(event: str, plugin_name: str, task: asyncio.Task[None], **fields: object) -> None:
    """Log a lifespan task that failed or is still running, cancelling the latter so it cannot hold anything up."""
    if not task.done():
        task.cancel()
        logger.error(event, plugin_name=plugin_name, timeout_seconds=_LIFESPAN_DEADLINE_SECONDS, **fields)
        return
    try:
        task.result()
    # Also when its code awaited a cancelled task without catching that.
    except (Exception, asyncio.CancelledError):
        logger.exception(event, plugin_name=plugin_name, **fields)


async def _close_websockets(routes_apps: Iterable[PluginRoutesApp]) -> None:
    """Close the open websockets of ``routes_apps``, giving up on those a stalled client keeps from closing in time."""
    with suppress(TimeoutError):
        async with asyncio.timeout(_LIFESPAN_DEADLINE_SECONDS):
            await asyncio.gather(*(routes_app.close_websockets() for routes_app in routes_apps))


@dataclass(eq=False)
class PluginLifespans:
    """The running lifespans of the plugins the primary runtime published, from their start to their stop."""

    runtime_paths: RuntimePaths
    # The config the runtime serves now.
    current_config: Callable[[], Config]
    # Primary runtime registry for plugin EventSources, or None outside the primary runtime.
    event_sources: EventSourceRegistry | None = None
    # The generation whose lifespans run.
    _published: PluginGeneration = field(default_factory=PluginGeneration, init=False)
    _running: dict[str, _RunningLifespan] = field(default_factory=dict, init=False)
    # Orders publications and the shutdown stop, so a publication shutdown interrupts still ends in that stop.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _stopped: bool = field(default=False, init=False)

    async def apply_published(self) -> None:
        """Follow the plugin generation published now, when it changed since the last call.

        Its lifespans start, then the replaced plugins' websockets close and their lifespans stop. Even if the caller
        is cancelled, this finishes.
        """
        await asyncio.shield(self._apply_published())

    async def _apply_published(self) -> None:
        async with self._lock:
            generation = config_plugins(self.current_config(), self.runtime_paths).generation
            if self._stopped or generation is self._published:
                return
            previous, self._published = self._published, generation
            replaced, self._running = self._running, {}
            for running in replaced.values():
                running.lifecycle._closed = True
            # The new code may start services as it starts, so those the replaced code started stop first.
            await stop_started_worker_services()
            for plugin in generation.plugins.values():
                if plugin.lifespan is None:
                    continue
                lifecycle = PluginLifecycle(
                    plugin.name,
                    plugin_process_state(plugin.name),
                    self.runtime_paths,
                    self.current_config,
                    self.event_sources,
                )
                running = await _start(plugin.lifespan, lifecycle)
                if running is not None:
                    self._running[plugin.name] = running
            # A primary load executes every plugin again, so each plugin of the previous generation is replaced.
            await _close_websockets(
                app
                for plugin in previous.plugins.values()
                for app in (plugin.routes_app, plugin.origin_app)
                if app is not None
            )
            for name, running in replaced.items():
                await _stop(running, "reload" if name in self._running else "disabled")

    async def stop(self) -> None:
        """Stop every running lifespan as MindRoom shuts down, and start none after."""
        async with self._lock:
            self._stopped = True
            running, self._running = self._running, {}
            for lifespan in running.values():
                await _stop(lifespan, "shutdown")
            await stop_started_worker_services()

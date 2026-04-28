"""Shared live runtime state exposed to extracted bot collaborators."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache, EventCacheWriteCoordinator
    from mindroom.runtime_protocols import (
        OrchestratorRuntime,
        SupportsClientConfig,
        SupportsClientConfigOrchestrator,
        SupportsConfig,
        SupportsConfigOrchestrator,
    )
    from mindroom.runtime_support import StartupThreadPrewarmRegistry


class BotRuntimeView(Protocol):
    """Live mutable bot state that extracted collaborators may consult."""

    @property
    def client(self) -> nio.AsyncClient | None: ...  # noqa: D102

    @property
    def config(self) -> Config: ...  # noqa: D102

    @property
    def runtime_paths(self) -> RuntimePaths: ...  # noqa: D102

    @property
    def enable_streaming(self) -> bool: ...  # noqa: D102

    @property
    def orchestrator(self) -> OrchestratorRuntime | None: ...  # noqa: D102

    @property
    def event_cache(self) -> ConversationEventCache: ...  # noqa: D102

    @property
    def event_cache_write_coordinator(self) -> EventCacheWriteCoordinator: ...  # noqa: D102

    @property
    def startup_thread_prewarm_registry(self) -> StartupThreadPrewarmRegistry: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102


@dataclass
class BotRuntimeState:
    """Concrete mutable runtime state shared by extracted collaborators."""

    client: nio.AsyncClient | None
    config: Config
    runtime_paths: RuntimePaths
    enable_streaming: bool
    orchestrator: OrchestratorRuntime | None
    event_cache: ConversationEventCache | None
    event_cache_write_coordinator: EventCacheWriteCoordinator | None
    startup_thread_prewarm_registry: StartupThreadPrewarmRegistry | None = None
    runtime_started_at: float = field(default_factory=time.time)

    def mark_runtime_started(self) -> None:
        """Record the runtime start time for this bot start."""
        self.runtime_started_at = time.time()


if TYPE_CHECKING:

    def _check_runtime_state_satisfies_narrow_protocols(
        view: BotRuntimeView,
    ) -> None:
        """Type-only proof that BotRuntimeView satisfies every narrow protocol.

        This function is never called at runtime. The assignments below
        will fail static type-check if any narrow protocol drifts out of
        the BotRuntimeView surface.
        """
        _a: SupportsConfig = view
        _b: SupportsClientConfig = view
        _c: SupportsConfigOrchestrator = view
        _d: SupportsClientConfigOrchestrator = view

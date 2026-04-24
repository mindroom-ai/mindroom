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
    from mindroom.orchestrator import MultiAgentOrchestrator
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
    def orchestrator(self) -> MultiAgentOrchestrator | None: ...  # noqa: D102

    @property
    def event_cache(self) -> ConversationEventCache: ...  # noqa: D102

    @property
    def event_cache_write_coordinator(self) -> EventCacheWriteCoordinator: ...  # noqa: D102

    @property
    def startup_thread_prewarm_registry(self) -> StartupThreadPrewarmRegistry: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102

    @property
    def pre_runtime_thread_cache_trusted(self) -> bool: ...  # noqa: D102


@dataclass
class BotRuntimeState:
    """Concrete mutable runtime state shared by extracted collaborators."""

    client: nio.AsyncClient | None
    config: Config
    runtime_paths: RuntimePaths
    enable_streaming: bool
    orchestrator: MultiAgentOrchestrator | None
    event_cache: ConversationEventCache | None
    event_cache_write_coordinator: EventCacheWriteCoordinator | None
    startup_thread_prewarm_registry: StartupThreadPrewarmRegistry | None = None
    runtime_started_at: float = field(default_factory=time.time)
    restored_sync_token: bool = False
    sync_catchup_applied_at: float | None = None

    @property
    def pre_runtime_thread_cache_trusted(self) -> bool:
        """Return whether pre-start thread snapshots passed Matrix sync catch-up."""
        return (
            self.restored_sync_token
            and self.sync_catchup_applied_at is not None
            and self.sync_catchup_applied_at >= self.runtime_started_at
        )

    def mark_runtime_started(self, *, restored_sync_token: bool = False) -> None:
        """Advance the runtime freshness boundary for one bot start or restart."""
        self.runtime_started_at = time.time()
        self.restored_sync_token = restored_sync_token
        self.sync_catchup_applied_at = None

    def mark_sync_catchup_applied(self) -> None:
        """Record that the first post-start Matrix sync has applied cache catch-up."""
        if self.restored_sync_token:
            self.sync_catchup_applied_at = time.time()

    def mark_restored_sync_token_invalid(self) -> None:
        """Fail closed for pre-runtime cache reuse after a bad persisted sync token."""
        self.restored_sync_token = False
        self.sync_catchup_applied_at = None

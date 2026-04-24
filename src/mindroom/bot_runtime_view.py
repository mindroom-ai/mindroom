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

    @property
    def pre_runtime_thread_cache_valid_after(self) -> float | None: ...  # noqa: D102

    @property
    def sync_token_persistence_suppressed(self) -> bool: ...  # noqa: D102

    def suppress_sync_token_persistence(self) -> None: ...  # noqa: D102


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
    sync_token_thread_cache_valid_after: float | None = None
    sync_catchup_applied_at: float | None = None
    sync_token_persistence_suppressed: bool = False
    sync_token_cache_catchup_pending: bool = False

    @property
    def pre_runtime_thread_cache_trusted(self) -> bool:
        """Return whether pre-start thread snapshots passed Matrix sync catch-up."""
        return (
            self.restored_sync_token
            and self.sync_token_thread_cache_valid_after is not None
            and self.sync_catchup_applied_at is not None
            and self.sync_catchup_applied_at >= self.runtime_started_at
        )

    @property
    def pre_runtime_thread_cache_valid_after(self) -> float | None:
        """Return the lower bound for restored-token durable thread-cache reuse."""
        if not self.pre_runtime_thread_cache_trusted:
            return None
        return self.sync_token_thread_cache_valid_after

    def thread_cache_valid_after_for_sync_token(self) -> float:
        """Return the durable cache boundary to carry into the next certified sync token."""
        if self.pre_runtime_thread_cache_trusted and self.sync_token_thread_cache_valid_after is not None:
            return self.sync_token_thread_cache_valid_after
        return self.runtime_started_at

    def mark_runtime_started(
        self,
        *,
        restored_sync_token: bool = False,
        thread_cache_valid_after: float | None = None,
    ) -> None:
        """Advance the runtime freshness boundary for one bot start or restart."""
        self.runtime_started_at = time.time()
        self.restored_sync_token = restored_sync_token
        self.sync_token_thread_cache_valid_after = thread_cache_valid_after if restored_sync_token else None
        self.sync_catchup_applied_at = None
        self.sync_token_persistence_suppressed = False
        self.sync_token_cache_catchup_pending = False

    def mark_sync_catchup_applied(self) -> None:
        """Record that the first post-start Matrix sync has applied cache catch-up."""
        if self.restored_sync_token:
            self.sync_catchup_applied_at = time.time()

    def mark_restored_sync_token_invalid(self) -> None:
        """Fail closed for pre-runtime cache reuse after a bad persisted sync token."""
        self.restored_sync_token = False
        self.sync_token_thread_cache_valid_after = None
        self.sync_catchup_applied_at = None

    def suppress_sync_token_persistence(self) -> None:
        """Prevent later same-runtime tokens from becoming future restored-token trust roots."""
        self.sync_token_persistence_suppressed = True

    def begin_sync_token_cache_catchup(self) -> None:
        """Block sync-token persistence until the current sync cache catch-up is certified."""
        self.sync_token_cache_catchup_pending = True

    def finish_sync_token_cache_catchup(self) -> None:
        """Allow token persistence again after current sync cache catch-up is certified."""
        self.sync_token_cache_catchup_pending = False

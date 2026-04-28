"""Small runtime Protocols for extracted bot collaborators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMatrixAdmin, HookMessageSender, HookRoomStatePutter, HookRoomStateQuerier
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.plugins import PluginReloadResult

__all__ = [
    "OrchestratorRuntime",
    "SupportsClientConfig",
    "SupportsClientConfigOrchestrator",
    "SupportsConfig",
    "SupportsConfigOrchestrator",
    "SupportsRunningState",
]


class SupportsRunningState(Protocol):
    """Expose whether a managed runtime actor is currently running."""

    running: bool


class OrchestratorRuntime(Protocol):
    """Narrow orchestrator surface used by extracted runtime collaborators."""

    @property
    def config(self) -> Config | None: ...  # noqa: D102

    @property
    def runtime_paths(self) -> RuntimePaths: ...  # noqa: D102

    @property
    def agent_bots(self) -> object: ...  # noqa: D102

    @property
    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler: ...  # noqa: D102

    def hook_message_sender(self) -> HookMessageSender | None: ...  # noqa: D102

    def hook_room_state_querier(self) -> HookRoomStateQuerier | None: ...  # noqa: D102

    def hook_room_state_putter(self) -> HookRoomStatePutter | None: ...  # noqa: D102

    def hook_matrix_admin(self) -> HookMatrixAdmin | None: ...  # noqa: D102

    def reload_plugins_now(self, *, source: str) -> Awaitable[PluginReloadResult]: ...  # noqa: D102


class SupportsConfig(Protocol):
    """Expose the runtime config snapshot."""

    @property
    def config(self) -> Config: ...  # noqa: D102


class SupportsClientConfig(Protocol):
    """Expose the Matrix client plus runtime config."""

    @property
    def client(self) -> nio.AsyncClient | None: ...  # noqa: D102

    @property
    def config(self) -> Config: ...  # noqa: D102


class SupportsConfigOrchestrator(SupportsConfig, Protocol):
    """Expose the config plus optional orchestrator handle."""

    @property
    def orchestrator(self) -> OrchestratorRuntime | None: ...  # noqa: D102


class SupportsClientConfigOrchestrator(SupportsClientConfig, Protocol):
    """Expose client/config access, orchestrator access, and runtime freshness."""

    @property
    def orchestrator(self) -> OrchestratorRuntime | None: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102

"""Small runtime Protocols for extracted bot collaborators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.orchestrator import MultiAgentOrchestrator

__all__ = [
    "SupportsClientConfig",
    "SupportsClientConfigOrchestrator",
    "SupportsConfig",
    "SupportsConfigOrchestrator",
]


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
    def orchestrator(self) -> MultiAgentOrchestrator | None: ...  # noqa: D102


class SupportsClientConfigOrchestrator(SupportsClientConfig, Protocol):
    """Expose client/config access plus the orchestrator."""

    @property
    def orchestrator(self) -> MultiAgentOrchestrator | None: ...  # noqa: D102


if TYPE_CHECKING:
    from mindroom.bot_runtime_view import BotRuntimeView

    def _check_narrow_protocols_are_subsets_of_bot_runtime_view(
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

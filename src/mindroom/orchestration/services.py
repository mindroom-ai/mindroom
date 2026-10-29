"""The lifecycle shared by runtime services that follow the active config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mindroom.config.main import Config


class RuntimeService(Protocol):
    """A long-running service that each config sync starts, reconfigures, or stops."""

    @property
    def name(self) -> str:
        """Return the name its shutdown step reports."""
        ...

    async def sync(self, config: Config) -> None:
        """Bring the service in line with ``config``."""
        ...

    async def stop(self) -> None:
        """Stop the service and wait until its work has ended."""
        ...

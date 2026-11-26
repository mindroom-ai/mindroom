"""Guard against ambient libpq routing environment variables overriding scoped PostgreSQL configs."""

from __future__ import annotations

import os


def reject_ambient_libpq_env(*, allow_hostaddr: bool = False) -> None:
    """Fail closed when ambient ``PGSERVICE`` or ``PGHOSTADDR`` would redirect a libpq connection."""
    leaked: list[str] = []
    if os.environ.get("PGSERVICE"):
        leaked.append("PGSERVICE")
    if not allow_hostaddr and os.environ.get("PGHOSTADDR"):
        leaked.append("PGHOSTADDR")
    if leaked:
        names = ", ".join(leaked)
        msg = (
            f"Ambient {names} is set; refusing to connect because libpq applies ambient routing overrides. "
            f"Unset {names} before connecting."
        )
        raise ValueError(msg)

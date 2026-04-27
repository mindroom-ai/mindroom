"""Public snapshot type for latest visible cached agent messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentMessageSnapshot:
    """Latest visible message content and timestamp for one sender."""

    content: dict[str, Any]
    origin_server_ts: int


class AgentMessageSnapshotUnavailable(RuntimeError):  # noqa: N818
    """Raised when an existing Matrix event cache cannot be safely read."""

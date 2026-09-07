"""The latest visible message one sender has in a conversation scope.

A leaf type on purpose: hooks consume it and the conversation projection
produces it, and neither should have to import the other to name it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentMessageSnapshot:
    """Latest visible message content and timestamp for one sender."""

    content: dict[str, Any]
    origin_server_ts: int

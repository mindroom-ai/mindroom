"""Knowledge availability states shared by read paths and refresh scheduling."""

from __future__ import annotations

from enum import Enum


class KnowledgeAvailability(Enum):
    """Availability state for one knowledge base on the request path."""

    READY = "ready"
    INITIALIZING = "initializing"
    REFRESH_FAILED = "refresh_failed"
    CONFIG_MISMATCH = "config_mismatch"

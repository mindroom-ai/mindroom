"""Compatibility contract shared by worker hosts and sandbox runners."""

from __future__ import annotations

WORKER_PROTOCOL_VERSION = 1


def worker_health_payload(*, mindroom_version: str) -> dict[str, str | int]:
    """Return the public worker readiness and compatibility payload."""
    return {
        "status": "ok",
        "mindroom_version": mindroom_version,
        "worker_protocol": WORKER_PROTOCOL_VERSION,
    }

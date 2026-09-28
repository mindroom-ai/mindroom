"""Compatibility contract shared by worker hosts and sandbox runners."""

from __future__ import annotations

from dataclasses import dataclass

# 2: workers mount only agent workspaces and never write private-instance identity records.
WORKER_PROTOCOL_VERSION = 2


@dataclass(frozen=True)
class _WorkerHealthPayload:
    """Public worker readiness and compatibility payload."""

    status: str
    mindroom_version: str
    worker_protocol: int


def worker_health_payload(*, mindroom_version: str) -> _WorkerHealthPayload:
    """Return the public worker readiness and compatibility payload."""
    return _WorkerHealthPayload(
        status="ok",
        mindroom_version=mindroom_version,
        worker_protocol=WORKER_PROTOCOL_VERSION,
    )

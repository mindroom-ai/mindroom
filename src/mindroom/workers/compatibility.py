"""Compatibility contract shared by worker hosts and sandbox runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

# 2: workers mount only agent workspaces and never write private-instance identity records.
# 3: /execute streams progress and keep-alive lines before its result line, and job calls have no time limit.
# 4: a shell call's set and removed variables (execution_env_set, execution_env_unset) apply over the worker's own
#    environment and workspace script, so a worker that ignored them would hand commands what a plugin removed.
_WORKER_PROTOCOL_VERSION = 4


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
        worker_protocol=_WORKER_PROTOCOL_VERSION,
    )


def worker_health_compatibility_error(
    response: httpx.Response,
    *,
    backend_label: str,
    image: str,
    older_worker_hint: str | None = None,
) -> str | None:
    """Return an actionable error when a ready worker's health check reports another protocol.

    ``older_worker_hint`` is appended only when the worker reports an older or no protocol.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if not isinstance(payload, dict):
        reported_protocol: object = "missing"
        reported_version: object = "unknown"
    else:
        reported_protocol = payload.get("worker_protocol", "missing")
        reported_version = payload.get("mindroom_version", "unknown")

    newer_worker = False
    if isinstance(reported_protocol, int) and not isinstance(reported_protocol, bool):
        if reported_protocol == _WORKER_PROTOCOL_VERSION:
            return None
        newer_worker = reported_protocol > _WORKER_PROTOCOL_VERSION
    mismatch = (
        f"{backend_label} worker image '{image}' is incompatible with this MindRoom runtime: "
        f"expected worker protocol {_WORKER_PROTOCOL_VERSION}, got {reported_protocol} "
        f"(worker MindRoom version {reported_version})."
    )
    if newer_worker:
        return (
            f"{mismatch} The worker image comes from a newer MindRoom release: run this runtime on the same release, "
            "for example by restarting it so it pulls the same mutable tag, or pin both to one release tag."
        )
    advice = f"{mismatch} Use a worker image built for this MindRoom release."
    return advice if older_worker_hint is None else f"{advice} {older_worker_hint}"

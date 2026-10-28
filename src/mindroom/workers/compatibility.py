"""Compatibility contract shared by worker hosts and sandbox runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION

if TYPE_CHECKING:
    import httpx


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


def worker_health_compatibility_error(
    response: httpx.Response,
    *,
    backend_label: str,
    image: str,
    older_worker_hint: str | None = None,
    newer_worker_hint: str | None = None,
) -> str | None:
    """Return an actionable error when a ready worker's health check reports another protocol.

    ``older_worker_hint`` is appended only when the worker reports an older or no protocol,
    and ``newer_worker_hint`` only when it reports a newer one.
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
        if reported_protocol == WORKER_PROTOCOL_VERSION:
            return None
        newer_worker = reported_protocol > WORKER_PROTOCOL_VERSION
    mismatch = (
        f"{backend_label} worker image '{image}' is incompatible with this MindRoom runtime: "
        f"expected worker protocol {WORKER_PROTOCOL_VERSION}, got {reported_protocol} "
        f"(worker MindRoom version {reported_version})."
    )
    if newer_worker:
        advice = (
            f"{mismatch} The worker image comes from a newer MindRoom release: "
            "upgrade this runtime to the worker's release, or pin both to one release tag."
        )
        hint = newer_worker_hint
    else:
        advice = f"{mismatch} Use a worker image built for this MindRoom release."
        hint = older_worker_hint
    return advice if hint is None else f"{advice} {hint}"

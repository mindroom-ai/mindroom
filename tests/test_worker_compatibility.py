"""Tests for the worker protocol check shared by the dedicated worker backends."""

from __future__ import annotations

import httpx
import pytest

from mindroom.workers.compatibility import _WORKER_PROTOCOL_VERSION, worker_health_compatibility_error


def test_worker_health_accepts_matching_protocol() -> None:
    """A worker using the host protocol should pass the compatibility handshake."""
    response = httpx.Response(
        200,
        json={
            "status": "ok",
            "mindroom_version": "2026.8.1",
            "worker_protocol": _WORKER_PROTOCOL_VERSION,
        },
    )

    assert worker_health_compatibility_error(response, backend_label="Docker", image="mindroom:2026.8.1") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok"},
        {"status": "ok", "mindroom_version": "2026.7.1", "worker_protocol": 0},
        # Workers from releases that mounted whole state roots write private identity records the new mounts forbid.
        {"status": "ok", "mindroom_version": "2026.9.324", "worker_protocol": 1},
        # Workers from releases before plugin environment removals would hand commands variables a plugin removed.
        {"status": "ok", "mindroom_version": "2026.10.580", "worker_protocol": 3},
        {"status": "ok", "mindroom_version": "2026.8.1", "worker_protocol": True},
        {"status": "ok", "mindroom_version": "2026.8.1", "worker_protocol": 1.0},
    ],
)
def test_worker_health_rejects_older_or_invalid_protocol(payload: dict[str, object]) -> None:
    """Missing and stale worker protocols should fail with image guidance."""
    response = httpx.Response(200, json=payload)

    error = worker_health_compatibility_error(response, backend_label="Docker", image="mindroom:stale")

    assert error is not None
    assert "mindroom:stale" in error
    assert f"expected worker protocol {_WORKER_PROTOCOL_VERSION}" in error
    assert "Use a worker image built for this MindRoom release" in error


def test_worker_health_sends_a_newer_worker_to_the_primary_release() -> None:
    """A worker from a newer release is fixed on the primary's side, so only the caller's newer-worker hint applies."""
    response = httpx.Response(
        200,
        json={"status": "ok", "mindroom_version": "2026.11.1", "worker_protocol": _WORKER_PROTOCOL_VERSION + 1},
    )

    error = worker_health_compatibility_error(
        response,
        backend_label="Docker",
        image="mindroom:latest",
        older_worker_hint="older-worker hint",
        newer_worker_hint="newer-worker hint",
    )

    assert error is not None
    assert f"got {_WORKER_PROTOCOL_VERSION + 1} (worker MindRoom version 2026.11.1)" in error
    assert error.endswith(
        "The worker image comes from a newer MindRoom release: "
        "upgrade this runtime to the worker's release, or pin both to one release tag. newer-worker hint",
    )
    assert "Use a worker image built for this MindRoom release" not in error
    assert "older-worker hint" not in error


def test_worker_health_appends_the_older_worker_hint_to_an_older_worker() -> None:
    """The caller's hint follows the advice for a worker from an older release."""
    response = httpx.Response(200, json={"status": "ok", "mindroom_version": "2026.10.580", "worker_protocol": 3})

    error = worker_health_compatibility_error(
        response,
        backend_label="Docker",
        image="mindroom:latest",
        older_worker_hint="older-worker hint",
        newer_worker_hint="newer-worker hint",
    )

    assert error is not None
    assert error.endswith("Use a worker image built for this MindRoom release. older-worker hint")
    assert "newer-worker hint" not in error


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, text="ok"), id="invalid-json"),
        pytest.param(httpx.Response(200, json=[]), id="non-object-json"),
    ],
)
def test_worker_health_rejects_malformed_payload(response: httpx.Response) -> None:
    """Malformed worker health responses should be treated as incompatible."""
    error = worker_health_compatibility_error(response, backend_label="Docker", image="mindroom:malformed")

    assert error is not None
    assert "got missing" in error
    assert "worker MindRoom version unknown" in error

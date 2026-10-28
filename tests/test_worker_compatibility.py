"""Tests for the worker protocol check shared by the worker backends."""

from __future__ import annotations

import httpx
import pytest

from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.compatibility import WorkerProtocolMismatch, ready_worker_protocol_mismatch
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION


def _first_check(response: httpx.Response, **kwargs: str) -> WorkerProtocolMismatch | None:
    """Check a ready worker that replaces no other worker."""
    return ready_worker_protocol_mismatch(response, replacing=False, **kwargs)


def test_worker_health_accepts_matching_protocol() -> None:
    """A worker using the host protocol should pass the compatibility handshake."""
    response = httpx.Response(
        200,
        json={
            "status": "ok",
            "mindroom_version": "2026.8.1",
            "worker_protocol": WORKER_PROTOCOL_VERSION,
        },
    )

    assert _first_check(response, worker="Docker worker image 'mindroom:2026.8.1'") is None
    assert ready_worker_protocol_mismatch(response, worker="Docker worker image 'mindroom'", replacing=True) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok"},
        {"status": "ok", "mindroom_version": "2026.7.1", "worker_protocol": 0},
        # Workers from releases that mounted whole state roots write private identity records the new mounts forbid.
        {"status": "ok", "mindroom_version": "2026.9.324", "worker_protocol": 1},
        # Workers from releases before plugin environment removals would hand commands variables a plugin removed.
        {"status": "ok", "mindroom_version": "2026.10.580", "worker_protocol": 3},
        # Workers from releases before plugin PATH directories would run a configured program in a wrapper's place.
        {"status": "ok", "mindroom_version": "2026.10.612", "worker_protocol": 4},
        {"status": "ok", "mindroom_version": "2026.8.1", "worker_protocol": True},
        {"status": "ok", "mindroom_version": "2026.8.1", "worker_protocol": 1.0},
    ],
)
def test_worker_health_rejects_older_or_invalid_protocol(payload: dict[str, object]) -> None:
    """Missing and stale worker protocols should fail with image guidance."""
    response = httpx.Response(200, json=payload)

    mismatch = _first_check(response, worker="Docker worker image 'mindroom:stale'")

    assert mismatch is not None
    assert not mismatch.worker_is_newer
    error = mismatch.error
    assert "mindroom:stale" in error
    assert f"expected worker protocol {WORKER_PROTOCOL_VERSION}" in error
    assert "Use a worker image built for this MindRoom release" in error


def test_worker_health_sends_a_newer_worker_to_the_primary_release() -> None:
    """A worker from a newer release is fixed on the primary's side, so only the caller's newer-worker hint applies."""
    response = httpx.Response(
        200,
        json={"status": "ok", "mindroom_version": "2026.11.1", "worker_protocol": WORKER_PROTOCOL_VERSION + 1},
    )

    mismatch = _first_check(
        response,
        worker="Docker worker image 'mindroom:latest'",
        older_worker_hint="older-worker hint",
        newer_worker_hint="newer-worker hint",
    )

    assert mismatch is not None
    assert mismatch.worker_is_newer
    error = mismatch.error
    assert f"got {WORKER_PROTOCOL_VERSION + 1} (worker MindRoom version 2026.11.1)" in error
    assert error.endswith(
        "The worker image comes from a newer MindRoom release: "
        "upgrade this runtime to the worker's release, or pin both to one release tag. newer-worker hint",
    )
    assert "Use a worker image built for this MindRoom release" not in error
    assert "older-worker hint" not in error


def test_worker_health_appends_the_older_worker_hint_to_an_older_worker() -> None:
    """The caller's hint follows the advice for a worker from an older release."""
    response = httpx.Response(200, json={"status": "ok", "mindroom_version": "2026.10.580", "worker_protocol": 3})

    mismatch = _first_check(
        response,
        worker="Docker worker image 'mindroom:latest'",
        older_worker_hint="older-worker hint",
        newer_worker_hint="newer-worker hint",
    )

    assert mismatch is not None
    error = mismatch.error
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
    mismatch = _first_check(response, worker="Docker worker image 'mindroom:malformed'")

    assert mismatch is not None
    assert "got missing" in mismatch.error
    assert "worker MindRoom version unknown" in mismatch.error


@pytest.mark.parametrize("reported_protocol", [3, WORKER_PROTOCOL_VERSION + 1], ids=["older", "newer"])
def test_a_replacement_worker_that_still_mismatches_fails_instead_of_being_replaced_again(
    reported_protocol: int,
) -> None:
    """A backend replaces a mismatched worker once; the replacement's mismatch is the error it reports."""
    response = httpx.Response(200, json={"status": "ok", "mindroom_version": "x", "worker_protocol": reported_protocol})
    first = _first_check(response, worker="Docker worker image 'mindroom:latest'")
    assert first is not None

    with pytest.raises(WorkerBackendError) as exc_info:
        ready_worker_protocol_mismatch(response, worker="Docker worker image 'mindroom:latest'", replacing=True)

    assert str(exc_info.value) == first.error

"""Tests for the paired install heartbeat to the hosted provisioning service."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix import provisioning_heartbeat
from mindroom.matrix.provisioning_heartbeat import run_provisioning_heartbeat

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PAIRED_ENV = {
    "MINDROOM_PROVISIONING_URL": "https://provisioning.example/",
    "MINDROOM_LOCAL_CLIENT_ID": "local-client",
    "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret",
}


class _StopLoopError(Exception):
    """Raised by the fake sleep to end the otherwise endless heartbeat loop."""


def _runtime_paths(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[list[httpx.Request], list[dict[str, object]]]:
    requests: list[httpx.Request] = []
    client_kwargs: list[dict[str, object]] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(_record)
    real_async_client = httpx.AsyncClient

    def _client_factory(**kwargs: object) -> httpx.AsyncClient:
        client_kwargs.append(kwargs)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(provisioning_heartbeat.httpx, "AsyncClient", _client_factory)
    return requests, client_kwargs


def _sleep_until(stop_after: int) -> tuple[list[float], Callable[[float], object]]:
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= stop_after:
            raise _StopLoopError

    return sleeps, _sleep


@pytest.mark.asyncio
async def test_heartbeat_reports_at_startup_and_then_every_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paired install reports immediately and after each interval, sending only its client credentials."""
    requests, client_kwargs = _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"status": "ok"}),
    )
    sleeps, sleep = _sleep_until(stop_after=3)

    with pytest.raises(_StopLoopError):
        await run_provisioning_heartbeat(_runtime_paths(tmp_path, _PAIRED_ENV), sleep=sleep)

    assert len(requests) == 3
    assert sleeps == [6 * 60 * 60] * 3
    for request in requests:
        assert request.method == "POST"
        assert request.url == "https://provisioning.example/v1/local-mindroom/heartbeat"
        assert request.headers["X-Local-MindRoom-Client-Id"] == "local-client"
        assert request.headers["X-Local-MindRoom-Client-Secret"] == "local-secret"
        assert request.content == b""
    assert all(kwargs["timeout"] == provisioning_heartbeat._HEARTBEAT_TIMEOUT_SECONDS for kwargs in client_kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(("ssl_verify", "expected_verify"), [(None, True), ("false", False)])
async def test_heartbeat_follows_matrix_ssl_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ssl_verify: str | None,
    expected_verify: bool,
) -> None:
    """Heartbeats verify TLS like other provisioning calls, honouring MATRIX_SSL_VERIFY."""
    _, client_kwargs = _install_transport(monkeypatch, lambda _request: httpx.Response(200, json={"status": "ok"}))
    _, sleep = _sleep_until(stop_after=1)
    env = _PAIRED_ENV if ssl_verify is None else {**_PAIRED_ENV, "MATRIX_SSL_VERIFY": ssl_verify}

    with pytest.raises(_StopLoopError):
        await run_provisioning_heartbeat(_runtime_paths(tmp_path, env), sleep=sleep)

    assert [kwargs["verify"] for kwargs in client_kwargs] == [expected_verify]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "Invalid local client credentials"}),
        httpx.Response(403, json={"detail": "Connection revoked"}),
    ],
    ids=["invalid", "revoked"],
)
async def test_rejected_credentials_warn_once_and_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    """Invalid or revoked credentials log one clear warning and stop heartbeating."""
    requests, _ = _install_transport(monkeypatch, lambda _request: response)
    sleeps, sleep = _sleep_until(stop_after=3)

    with capture_logs() as logs:
        await run_provisioning_heartbeat(_runtime_paths(tmp_path, _PAIRED_ENV), sleep=sleep)

    assert len(requests) == 1
    assert sleeps == []
    assert [log for log in logs if log["log_level"] == "warning"] == [
        {"event": provisioning_heartbeat._REJECTED_WARNING, "log_level": "warning"},
    ]


@pytest.mark.asyncio
async def test_older_service_without_heartbeat_endpoint_adds_no_mindroom_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that predates the endpoint answers 404; MindRoom logs nothing beyond httpx's usual request line."""
    requests, _ = _install_transport(monkeypatch, lambda _request: httpx.Response(404, json={"detail": "Not Found"}))
    _, sleep = _sleep_until(stop_after=2)

    with capture_logs() as logs, pytest.raises(_StopLoopError):
        await run_provisioning_heartbeat(_runtime_paths(tmp_path, _PAIRED_ENV), sleep=sleep)

    assert len(requests) == 2
    assert logs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_or_error",
    [httpx.Response(500, text="boom"), httpx.Response(403, json={"detail": "Forbidden"}), httpx.ConnectError("down")],
)
async def test_heartbeat_failures_are_debug_only_and_keep_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_or_error: httpx.Response | httpx.HTTPError,
) -> None:
    """Transient failures never surface above debug and never end the loop."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(response_or_error, httpx.HTTPError):
            raise response_or_error
        return response_or_error

    requests, _ = _install_transport(monkeypatch, _handler)
    _, sleep = _sleep_until(stop_after=2)

    with capture_logs() as logs, pytest.raises(_StopLoopError):
        await run_provisioning_heartbeat(_runtime_paths(tmp_path, _PAIRED_ENV), sleep=sleep)

    assert len(requests) == 2
    assert logs
    assert {log["log_level"] for log in logs} == {"debug"}


@pytest.mark.asyncio
async def test_invalid_provisioning_url_is_debug_only_and_keeps_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed provisioning URL raises httpx.InvalidURL, which must not end the task with an exception."""
    requests, _ = _install_transport(monkeypatch, lambda _request: httpx.Response(200))
    sleeps, sleep = _sleep_until(stop_after=2)
    env = {**_PAIRED_ENV, "MINDROOM_PROVISIONING_URL": "https://provisioning.example:abc"}

    with capture_logs() as logs, pytest.raises(_StopLoopError):
        await run_provisioning_heartbeat(_runtime_paths(tmp_path, env), sleep=sleep)

    assert requests == []
    assert sleeps == [6 * 60 * 60] * 2
    assert [(log["event"], log["log_level"]) for log in logs] == [("Provisioning heartbeat failed", "debug")] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"MINDROOM_PROVISIONING_URL": "https://provisioning.example"},
        {"MINDROOM_LOCAL_CLIENT_ID": "local-client", "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret"},
        {"MINDROOM_PROVISIONING_URL": "https://provisioning.example", "MINDROOM_LOCAL_CLIENT_ID": "local-client"},
    ],
)
async def test_unpaired_install_sends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    """Without a provisioning URL and complete client credentials there is nothing to report."""
    requests, _ = _install_transport(monkeypatch, lambda _request: httpx.Response(200))
    sleeps, sleep = _sleep_until(stop_after=1)

    await run_provisioning_heartbeat(_runtime_paths(tmp_path, env), sleep=sleep)

    assert requests == []
    assert sleeps == []

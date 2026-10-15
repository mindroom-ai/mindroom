"""The primary reads a worker call's streamed progress, keep-alives, and result line."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest

from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_system.progress import tool_progress_reporter
from mindroom.tool_system.worker_proxy_client import (
    WorkerCallCancellation,
    WorkerProxyClientConfig,
    _ndjson_lines,
    execute_worker_proxy_request,
)
from mindroom.workers.in_flight import touch_workers_in_flight
from mindroom.workers.models import WorkerHandle

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class _Manager:
    cleanup_locator = "local:test"

    def __init__(self) -> None:
        self.touched: list[str] = []
        self.failures: list[str] = []

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> None:
        del now
        self.touched.append(worker_key)

    def record_failure(self, worker_key: str, failure_reason: str, **_kwargs: object) -> None:
        del worker_key
        self.failures.append(failure_reason)


class _StreamedResponse:
    status_code = 200

    def __init__(self, lines: Callable[[], Iterator[str]]) -> None:
        self._lines = lines
        self.closed = False

    def raise_for_status(self) -> None:
        return

    def read(self) -> bytes:
        return b""

    def iter_bytes(self) -> Iterator[bytes]:
        for line in self._lines():
            yield line.encode() + b"\n"


def _client(lines: Callable[[dict[str, Any]], Iterator[str]], seen: list[dict[str, Any]]) -> type:
    class _Client:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return

        @contextmanager
        def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[_StreamedResponse]:  # noqa: ANN401
            assert method == "POST"
            assert url == "http://worker/api/sandbox-runner/execute"
            seen.append(kwargs)
            response = _StreamedResponse(lambda: lines(kwargs["json"]))
            try:
                yield response
            finally:
                response.closed = True

    return _Client


_HANDLE = WorkerHandle(
    worker_id="worker-1",
    worker_key="agent:test",
    endpoint="http://worker/api/sandbox-runner/execute",
    auth_token="token",  # noqa: S106
    status="ready",
    backend_name="docker",
    last_used_at=0.0,
    created_at=0.0,
)


def _execute(
    lines: Callable[[dict[str, Any]], Iterator[str]],
    *,
    manager: _Manager | None = None,
    seen: list[dict[str, Any]] | None = None,
    timeout: float = 7.0,
    cancellation: WorkerCallCancellation | None = None,
) -> object:
    return execute_worker_proxy_request(
        config=WorkerProxyClientConfig(
            proxy_url=None,
            proxy_token=None,
            proxy_timeout_seconds=timeout,
            credential_lease_ttl_seconds=60,
            credential_policy={},
            lease_tool_credentials=False,
        ),
        payload={"tool_name": "shell", "function_name": "run_shell_command"},
        credentials_manager=None,
        tool_name="shell",
        function_name="run_shell_command",
        worker_target=None,
        worker_handle=_HANDLE,
        worker_manager=manager or _Manager(),
        client_factory=_client(lines, [] if seen is None else seen),
        cancellation=cancellation,
    )


def _result(result: object) -> str:
    return json.dumps({"ok": True, "result": result, "error": None, "failure_kind": None})


def test_progress_and_keepalives_come_before_the_result() -> None:
    """Progress reaches whoever owns the call; keep-alives only keep the stream open."""
    reports: list[str] = []

    def lines(_payload: dict[str, Any]) -> Iterator[str]:
        yield json.dumps({"progress": "step 1"})
        yield json.dumps({"keepalive": True})
        yield json.dumps({"progress": "step 2"})
        yield _result("done")

    with tool_progress_reporter(reports.append):
        assert _execute(lines) == "done"
    assert reports == ["step 1", "step 2"]


def test_a_stream_that_ends_without_a_result_is_a_worker_failure() -> None:
    """A runner that died mid-call leaves no result line, which says the worker failed."""
    manager = _Manager()

    def lines(_payload: dict[str, Any]) -> Iterator[str]:
        yield json.dumps({"keepalive": True})

    with pytest.raises(RuntimeError, match="ended without a result"):
        _execute(lines, manager=manager)
    assert manager.failures == ["Sandbox proxy stream ended without a result."]


def test_only_a_job_call_runs_past_its_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call that holds a reply keeps its limit however many keep-alives arrive; a job call has none."""
    clock = iter(range(100))
    monkeypatch.setattr("mindroom.tool_system.worker_proxy_client.time.monotonic", lambda: float(next(clock)))

    def lines(payload: dict[str, Any]) -> Iterator[str]:
        for _ in range(20):
            yield json.dumps({"keepalive": True})
        yield _result(payload["run_as_job"])

    manager = _Manager()
    with pytest.raises(httpx.ReadTimeout):
        _execute(lines, manager=manager, timeout=5.0)
    assert manager.failures == []
    with job_control_context(JobControl()):
        assert _execute(lines, timeout=5.0) is True


def test_a_cancelled_call_stops_reading_at_the_next_frame() -> None:
    """Once its caller stops waiting, the call drops its stream, which stops it at the runner."""
    cancellation = WorkerCallCancellation()
    read_after_cancel = threading.Event()

    def lines(_payload: dict[str, Any]) -> Iterator[str]:
        cancellation.cancel()
        yield json.dumps({"keepalive": True})
        read_after_cancel.set()
        yield _result("too late")

    with pytest.raises(asyncio.CancelledError):
        _execute(lines, cancellation=cancellation)
    assert not read_after_cancel.is_set()


def test_a_call_keeps_its_worker_from_idle_cleanup_while_it_runs() -> None:
    """Idle cleanup touches every worker with a call in flight first, and only those."""
    during: list[str] = []
    manager = _Manager()

    def lines(_payload: dict[str, Any]) -> Iterator[str]:
        touch_workers_in_flight(manager)
        during.extend(manager.touched)
        yield _result("done")

    _execute(lines, manager=manager)
    manager.touched.clear()
    touch_workers_in_flight(manager)

    assert during == ["agent:test"]
    assert manager.touched == []


def test_a_result_with_unicode_line_separators_arrives_whole() -> None:
    """Only a newline ends a streamed line, so text with U+2028, U+2029, or U+0085 in it is one result."""
    text = "a\u2028b\u2029c\x85d"
    body = (
        json.dumps({"keepalive": True}).encode()
        + b"\n"
        + json.dumps(
            {"ok": True, "result": text, "error": None, "failure_kind": None},
            ensure_ascii=False,
        ).encode()
    )
    manager = _Manager()

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return

        @contextmanager
        def stream(self, method: str, url: str, **_kwargs: Any) -> Iterator[httpx.Response]:  # noqa: ANN401
            yield httpx.Response(200, content=body, request=httpx.Request(method, url))

    result = execute_worker_proxy_request(
        config=WorkerProxyClientConfig(
            proxy_url=None,
            proxy_token=None,
            proxy_timeout_seconds=7.0,
            credential_lease_ttl_seconds=60,
            credential_policy={},
            lease_tool_credentials=False,
        ),
        payload={"tool_name": "shell", "function_name": "run_shell_command"},
        credentials_manager=None,
        tool_name="shell",
        function_name="run_shell_command",
        worker_target=None,
        worker_handle=_HANDLE,
        worker_manager=manager,
        client_factory=_Client,
    )

    assert result == text
    assert manager.failures == []


def test_streamed_lines_survive_any_chunk_boundary() -> None:
    """A line, or a character inside it, split across network chunks still arrives whole."""
    chunks = [b'{"a": "\xe2\x80', b'\xa8x"}\n{"b"', b": 1}\n", b'{"c": 2}']

    assert list(_ndjson_lines(iter(chunks))) == ['{"a": "\u2028x"}', '{"b": 1}', '{"c": 2}']

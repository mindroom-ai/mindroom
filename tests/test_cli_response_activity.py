"""Tests for the active-response CLI contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

import mindroom.cli.response_activity as response_activity_cli
from mindroom.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _snapshot(**overrides: object) -> dict[str, object]:
    return {
        "status": "idle",
        "runtime_phase": "ready",
        "admission_paused": False,
        "active_matrix_operations": 0,
        "active_openai_requests": 0,
        "active_calls": 0,
        "interruptible_script_runs": 0,
        "recoverable_script_runs": 0,
        **overrides,
    }


def _detailed_snapshot(**overrides: object) -> dict[str, object]:
    return _snapshot(**{"responses": [], "script_runs": [], **overrides})


def _without(field: str) -> dict[str, object]:
    payload = _snapshot()
    del payload[field]
    return payload


@pytest.mark.parametrize(
    ("payload", "exit_code", "status"),
    [
        (_snapshot(), 0, "idle"),
        (_snapshot(active_matrix_operations=2, status="busy"), 1, "busy"),
        (_snapshot(active_openai_requests=1, status="busy"), 1, "busy"),
        (_snapshot(active_calls=1, status="busy"), 1, "busy"),
        (_snapshot(interruptible_script_runs=1, status="busy"), 1, "busy"),
        (_snapshot(recoverable_script_runs=2), 0, "idle"),
        (_snapshot(runtime_phase="starting", status="unavailable"), 2, "unavailable"),
        (_snapshot(admission_paused=True, status="unavailable"), 2, "unavailable"),
        (_snapshot(active_matrix_operations=None, admission_paused=None, status="unavailable"), 2, "unavailable"),
        (_snapshot(active_calls=None, status="unavailable"), 2, "unavailable"),
        (
            _snapshot(interruptible_script_runs=None, recoverable_script_runs=None, status="unavailable"),
            2,
            "unavailable",
        ),
    ],
)
def test_cli_exit_codes_and_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, object],
    exit_code: int,
    status: str,
) -> None:
    """Only a ready, bound, idle snapshot permits exit zero."""
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)["status"] == status


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"status": "idle"},
        _snapshot(active_matrix_operations=-1),
        _snapshot(active_openai_requests=True),
        _snapshot(active_matrix_operations="0"),
        _snapshot(admission_paused="false"),
        _snapshot(active_calls=-1),
        _snapshot(recoverable_script_runs=False),
        _without("active_calls"),
        _without("interruptible_script_runs"),
        _without("recoverable_script_runs"),
    ],
)
def test_cli_rejects_incomplete_or_malformed_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
) -> None:
    """Missing or invalid counters must never be treated as zero."""
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 503, 302])
def test_cli_http_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
) -> None:
    """Auth, old servers, redirects, and server failures cannot return idle."""
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(status_code, text="not a snapshot"))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_cli_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stalled API must yield an unavailable result, not a traceback."""

    def fail(*_args: object, **_kwargs: object) -> httpx.Response:
        message = "stalled"
        raise httpx.ReadTimeout(message)

    monkeypatch.setattr(httpx, "get", fail)
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:9876", "http://127.0.0.1:9876", "http://[::1]:9876", "https://remote.test"],
)
def test_cli_uses_selected_environment_and_explicit_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    base_url: str,
) -> None:
    """Selected env credentials and explicit URL must reach the API without leaking the token."""
    (tmp_path / ".env").write_text("MINDROOM_URL=http://unused.test\nMINDROOM_API_KEY=test-secret\n")
    monkeypatch.delenv("MINDROOM_API_KEY", raising=False)
    monkeypatch.delenv("MINDROOM_URL", raising=False)

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url == f"{base_url}/base/api/responses/activity"
        assert kwargs["headers"] == {"Authorization": "Bearer test-secret"}
        assert kwargs["timeout"] == 3.0
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json=_snapshot())

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(
        app,
        [
            "check-active-responses",
            "--config",
            str(tmp_path / "config.yaml"),
            "--url",
            f"{base_url}/base/",
            "--timeout",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No active responses" in result.stdout
    assert "test-secret" not in result.output


@pytest.mark.parametrize(
    "url",
    ["http://remote.test", "http://10.0.0.1", "http://localhost.remote.test", "http://[::2]"],
)
def test_cli_rejects_credentials_over_remote_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    url: str,
) -> None:
    """A remote probe must not transmit credentials in cleartext."""
    monkeypatch.setenv("MINDROOM_API_KEY", "test-secret")
    sent: list[str] = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        sent.append(url)
        return httpx.Response(200, json=_snapshot())

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--url", url, "--json"],
    )
    assert result.exit_code == 2
    assert not sent
    assert "HTTPS" in json.loads(result.stdout)["detail"]
    assert "test-secret" not in result.output


@pytest.mark.parametrize(
    "option",
    [
        ["--url", "http://localhost:bad"],
        ["--url", "file:///activity"],
        ["--url", "http://secret@localhost"],
        ["--wait", "inf"],
        ["--wait", "nan"],
    ],
)
def test_cli_invalid_option_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, option: list[str]) -> None:
    """Bad URLs and unbounded waits must produce a controlled unavailable result without polling or exposing input."""
    served = _serve(monkeypatch, [httpx.Response(200, json=_snapshot())])
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), *option, "--json"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert not served
    assert "secret@" not in result.output


@pytest.mark.parametrize("wire_status", ["busy", "unavailable", "future-status", None])
def test_cli_rejects_conflicting_or_missing_wire_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wire_status: str | None,
) -> None:
    """Server-reported uncertainty must never be overwritten by idle counters."""
    payload = _snapshot()
    if wire_status is None:
        payload.pop("status", None)
    else:
        payload["status"] = wire_status
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_cli_details_json_uses_operator_key_and_detailed_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Detailed JSON preserves validated identity rows and uses the protected route."""
    monkeypatch.setenv("MINDROOM_API_KEY", "test-secret")
    payload = _detailed_snapshot(
        status="busy",
        active_openai_requests=2,
        responses=[
            {
                "channel": "openai",
                "responder": "helper",
                "requester_id": "@alice:example.org",
            },
        ],
    )

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url.endswith("/api/responses/activity/details")
        assert kwargs["headers"] == {"Authorization": "Bearer test-secret"}
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--details", "--json"],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == payload


def test_cli_details_text_labels_unknown_identities(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Human output names each channel and identity without hiding unknowns."""
    monkeypatch.setenv("MINDROOM_API_KEY", "test-secret")
    payload = _detailed_snapshot(
        status="busy",
        active_matrix_operations=1,
        active_openai_requests=1,
        responses=[
            {"channel": "matrix", "responder": "helper", "requester_id": None},
            {"channel": "openai", "responder": None, "requester_id": None},
        ],
    )
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--details"],
    )
    assert result.exit_code == 1, result.output
    assert "Matrix: helper for unknown requester" in result.stdout
    assert "OpenAI: unknown responder for unknown requester" in result.stdout


def test_cli_details_requires_key_before_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Detailed mode gives an actionable local error and sends no unauthenticated request."""
    monkeypatch.delenv("MINDROOM_API_KEY", raising=False)
    requested = False

    def get(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json=_detailed_snapshot())

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--details", "--json"],
    )
    assert result.exit_code == 2
    assert requested is False
    assert "MINDROOM_API_KEY" in json.loads(result.stdout)["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        _snapshot(),
        _detailed_snapshot(
            status="busy",
            active_openai_requests=1,
            responses=[{"channel": "openai", "responder": 1, "requester_id": None}],
        ),
        _detailed_snapshot(
            status="busy",
            active_openai_requests=1,
            responses=[{"channel": "invalid", "responder": None, "requester_id": None}],
        ),
    ],
)
def test_cli_details_rejects_aggregate_and_invalid_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    """Detailed mode requires response rows with valid channel and identity fields."""
    monkeypatch.setenv("MINDROOM_API_KEY", "test-secret")
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--details", "--json"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_cli_details_wrong_key_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A rejected detailed operator key remains an unavailable CLI result."""
    monkeypatch.setenv("MINDROOM_API_KEY", "wrong-key")
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(401, text="unauthorized"))
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--details", "--json"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert "wrong-key" not in result.output


def test_cli_text_reports_calls_and_recoverable_scripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Busy text names every counted source and keeps recoverable runs visible but uncounted."""
    payload = _snapshot(status="busy", active_calls=1, interruptible_script_runs=2, recoverable_script_runs=3)
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 1, result.output
    assert "1 call(s), 2 interruptible script run(s)" in result.stdout
    assert "Not counted as busy: 3 recoverable script run(s)." in result.stdout


def test_cli_details_text_lists_calls_and_script_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Call rows match response rows, and script rows state their restart outcome."""
    monkeypatch.setenv("MINDROOM_API_KEY", "test-secret")
    payload = _detailed_snapshot(
        status="busy",
        active_calls=1,
        interruptible_script_runs=1,
        recoverable_script_runs=1,
        responses=[{"channel": "call", "responder": "helper", "requester_id": "@alice:example.org"}],
        script_runs=[
            {"run_id": "script-1", "responder": "watcher", "requester_id": "@bob:example.org", "recoverable": False},
            {"run_id": "script-2", "responder": "watcher", "requester_id": "@bob:example.org", "recoverable": True},
        ],
    )
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--details"])
    assert result.exit_code == 1, result.output
    assert "Call: helper for @alice:example.org" in result.stdout
    assert "Script: watcher for @bob:example.org (script-1, interrupted by restart)" in result.stdout
    assert "Script: watcher for @bob:example.org (script-2, recoverable)" in result.stdout


class _FakeClock:
    """Advance monotonic time only through the command's own sleeps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(response_activity_cli, "monotonic", lambda: self.now)
        monkeypatch.setattr(response_activity_cli, "sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _serve(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> list[httpx.Response]:
    """Serve one queued response per poll, repeating the last one."""
    served: list[httpx.Response] = []

    def get(*_args: object, **_kwargs: object) -> httpx.Response:
        response = responses[min(len(served), len(responses) - 1)]
        served.append(response)
        return response

    monkeypatch.setattr(httpx, "get", get)
    return served


def test_cli_wait_polls_until_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A busy runtime that drains within the wait exits zero with the idle snapshot."""
    clock = _FakeClock(monkeypatch)
    busy = httpx.Response(200, json=_snapshot(status="busy", active_calls=1))
    served = _serve(monkeypatch, [busy, busy, httpx.Response(200, json=_snapshot())])
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--wait", "60", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "idle"
    assert len(served) == 3
    assert clock.sleeps == [5.0, 5.0]


def test_cli_wait_reports_busy_at_the_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Waiting never sleeps past the deadline and keeps the busy exit code."""
    clock = _FakeClock(monkeypatch)
    served = _serve(monkeypatch, [httpx.Response(200, json=_snapshot(status="busy", interruptible_script_runs=1))])
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--wait", "12", "--json"],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["status"] == "busy"
    assert clock.sleeps == [5.0, 5.0, 2.0]
    assert len(served) == 4


@pytest.mark.parametrize(
    "unavailable",
    [
        httpx.Response(503, json=_snapshot(admission_paused=True, status="unavailable")),
        httpx.Response(500, text="server error"),
        httpx.Response(200, json=_without("active_calls")),
    ],
)
def test_cli_wait_stops_on_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unavailable: httpx.Response,
) -> None:
    """An unknown status mid-wait ends with exit 2, even if a later poll would be idle."""
    _FakeClock(monkeypatch)
    busy = httpx.Response(200, json=_snapshot(status="busy", active_openai_requests=1))
    served = _serve(monkeypatch, [busy, unavailable, httpx.Response(200, json=_snapshot())])
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--wait", "60", "--json"],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert len(served) == 2

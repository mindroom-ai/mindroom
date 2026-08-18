"""Tests for the stdlib-only background-script SDK."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from typing import TYPE_CHECKING

import pytest

from mindroom.script_sdk import MindRoomToolCallError, MindRoomTools

if TYPE_CHECKING:
    from pathlib import Path
    from urllib.request import Request


def _arguments_digest(arguments: dict[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt(
    state: str,
    *,
    result: object | None = None,
    error: object | None = None,
    arguments: dict[str, object] | None = None,
    toolkit_name: str = "website",
    function_name: str = "read_url",
) -> bytes:
    receipt_arguments = {"url": "https://example.org/"} if arguments is None else arguments
    return json.dumps(
        {
            "run_id": "run-1",
            "call_id": "stable-call",
            "toolkit_name": toolkit_name,
            "function_name": function_name,
            "arguments_digest": _arguments_digest(receipt_arguments),
            "state": state,
            "created_at": "2026-08-18T00:00:00Z",
            "updated_at": "2026-08-18T00:00:01Z",
            "result": result,
            "error": error,
        },
    ).encode()


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_path = tmp_path / "capability"
    token_path.write_text("secret-token\n", encoding="utf-8")
    monkeypatch.setenv("MINDROOM_SCRIPT_GATEWAY_URL", "http://primary:8765/api/script-gateway")
    monkeypatch.setenv("MINDROOM_SCRIPT_RUN_ID", "run-1")
    monkeypatch.setenv("MINDROOM_SCRIPT_TOKEN_PATH", str(token_path))


def test_script_sdk_polls_the_same_accepted_call_id_until_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pending polling must retain the POST call ID instead of creating a second logical call."""
    _configure(monkeypatch, tmp_path)
    requests: list[Request] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        requests.append(request)
        if request.method == "POST":
            payload = json.loads(request.data or b"{}")
            assert payload["call_id"] == "stable-call"
            return io.BytesIO(_receipt("pending"))
        assert request.full_url.endswith("/runs/run-1/calls/stable-call")
        return io.BytesIO(_receipt("completed", result={"status": "ok"}))

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    result = MindRoomTools(poll_interval_seconds=0).call(
        "website",
        "read_url",
        url="https://example.org/",
    )

    assert result == {"status": "ok"}
    assert [request.method for request in requests] == ["POST", "GET"]
    assert all(request.headers["Authorization"] == "Bearer secret-token" for request in requests)


def test_script_sdk_ambiguous_submit_polls_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transport failure after POST may mean accepted, so retry must switch to GET for the same ID."""
    _configure(monkeypatch, tmp_path)
    methods: list[str] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        methods.append(request.method)
        if request.method == "POST":
            reason = "connection reset"
            raise urllib.error.URLError(reason)
        assert request.full_url.endswith("/runs/run-1/calls/stable-call")
        return io.BytesIO(_receipt("completed", result="page body"))

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    result = MindRoomTools(poll_interval_seconds=0).call("website", "read_url", url="https://example.org/")

    assert result == "page body"
    assert methods == ["POST", "GET"]


def test_script_sdk_retryable_submit_http_failure_polls_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A gateway failure after POST dispatch is ambiguous and may only be resolved by polling its call ID."""
    _configure(monkeypatch, tmp_path)
    methods: list[str] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        methods.append(request.method)
        if request.method == "POST":
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(b'{"detail":"temporarily unavailable"}'),
            )
        return io.BytesIO(_receipt("completed", result="accepted earlier"))

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    result = MindRoomTools(poll_interval_seconds=0).call("website", "read_url", url="https://example.org/")

    assert result == "accepted earlier"
    assert methods == ["POST", "GET"]


@pytest.mark.parametrize(
    ("receipt_toolkit", "receipt_function", "receipt_arguments"),
    [
        ("other", "read_url", {"url": "https://example.org/"}),
        ("website", "other", {"url": "https://example.org/"}),
        ("website", "read_url", {"url": "https://old.example/"}),
    ],
)
def test_script_sdk_rejects_old_conflicting_receipt_after_ambiguous_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_toolkit: str,
    receipt_function: str,
    receipt_arguments: dict[str, object],
) -> None:
    """Polling after ambiguous acceptance cannot consume a different call identity."""
    _configure(monkeypatch, tmp_path)
    methods: list[str] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        methods.append(request.method)
        if request.method == "POST":
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(b'{"detail":"acceptance not yet determined"}'),
            )
        return io.BytesIO(
            _receipt(
                "completed",
                result="old result",
                arguments=receipt_arguments,
                toolkit_name=receipt_toolkit,
                function_name=receipt_function,
            ),
        )

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0).call(
            "website",
            "read_url",
            url="https://example.org/",
        )

    assert exc_info.value.kind == "stable_call_conflict"
    assert exc_info.value.retryable is False
    assert methods == ["POST", "GET"]


def test_script_sdk_raises_stable_terminal_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A terminal broker failure must retain its failure kind and retryability."""
    _configure(monkeypatch, tmp_path)

    def urlopen(_request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        return io.BytesIO(
            _receipt(
                "failed",
                error={"kind": "capability_revoked", "message": "revoked", "retryable": False},
            ),
        )

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0).call("website", "read_url", url="https://example.org/")

    assert exc_info.value.kind == "capability_revoked"
    assert exc_info.value.retryable is False
    assert exc_info.value.call_id == "stable-call"


def test_script_sdk_returns_ordinary_completed_decline_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ordinary approval denial is a tool result, not an SDK exception."""
    _configure(monkeypatch, tmp_path)
    declined = (
        "[TOOL CALL DECLINED]\n"
        "Tool: read_url\n"
        "Reason: Not this time.\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )

    def urlopen(_request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        return io.BytesIO(_receipt("completed", result=declined, arguments={}))

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    assert MindRoomTools(poll_interval_seconds=0).call("website", "read_url") == declined


@pytest.mark.parametrize("removed_state", ["declined", "cancelled"])
def test_script_sdk_rejects_removed_call_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    removed_state: str,
) -> None:
    """Legacy call-only states must not be accepted as current gateway receipts."""
    _configure(monkeypatch, tmp_path)

    def urlopen(_request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        return io.BytesIO(_receipt(removed_state))

    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())
    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0).call("website", "read_url", url="https://example.org/")

    assert exc_info.value.kind == "invalid_response"

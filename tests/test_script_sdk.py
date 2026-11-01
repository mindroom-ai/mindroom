"""Tests for the stdlib-only background-script SDK."""

from __future__ import annotations

import io
import json
import urllib.error
from typing import TYPE_CHECKING

import pytest

from mindroom.script_runs.launch_contract import MAX_TOKEN_BYTES, digest_arguments
from mindroom.script_sdk import MindRoomToolCallError, MindRoomTools

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from urllib.request import Request


def _receipt(
    state: object,
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
            "arguments_digest": digest_arguments(receipt_arguments),
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
    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "stable-call"})())


def test_script_sdk_rejects_nonpositive_poll_interval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A zero interval must fail explicitly instead of flooding receipt endpoints."""
    _configure(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="poll_interval_seconds must be positive"):
        MindRoomTools(poll_interval_seconds=0)


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

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    result = MindRoomTools(poll_interval_seconds=0.0001).call(
        "website",
        "read_url",
        url="https://example.org/",
    )

    assert result == {"status": "ok"}
    assert [request.method for request in requests] == ["POST", "GET"]
    assert all(request.headers["Authorization"] == "Bearer secret-token" for request in requests)


def test_argument_digest_encoding_stays_stable_across_versions() -> None:
    """A script adopted across a primary upgrade digests its calls with its own, older SDK.

    Changing this value is a script SDK gateway contract change: bump ``SCRIPT_PROTOCOL_VERSION`` with it.
    """
    arguments: dict[str, object] = {"text": "héllo ✓", "nested": {"z": [1.5, None, True], "a": 10**20}, "count": -3}

    assert digest_arguments(arguments) == "dedbd5720dd68915a037c21e8e520036d4e60d7c139ccdbdf672580300819346"


def test_script_sdk_digests_the_json_wire_argument_representation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Nested integer keys must retain the gateway's post-JSON argument identity."""
    _configure(monkeypatch, tmp_path)
    posted_arguments: dict[str, object] = {}

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        payload = json.loads(request.data or b"{}")
        posted_arguments.update(payload["arguments"])
        return io.BytesIO(
            _receipt(
                "completed",
                result="accepted",
                arguments=payload["arguments"],
            ),
        )

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    result = MindRoomTools(poll_interval_seconds=0.0001).call(
        "website",
        "read_url",
        payload={1: "one", 10: "ten", 2: "two"},
    )

    assert result == "accepted"
    assert posted_arguments == {"payload": {"1": "one", "10": "ten", "2": "two"}}


@pytest.mark.parametrize(
    "invalid_arguments",
    [
        {"payload": {1: "integer", "2": "string"}},
        {"payload": float("nan")},
        {"payload": object()},
        {"payload": "\udcff"},
    ],
)
def test_script_sdk_rejects_invalid_arguments_before_post(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_arguments: dict[str, object],
) -> None:
    """Ambiguous or non-JSON arguments must fail locally without dispatching a call."""
    _configure(monkeypatch, tmp_path)
    requests: list[Request] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        requests.append(request)
        return io.BytesIO(b"{}")

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0.0001).call("website", "read_url", **invalid_arguments)

    assert exc_info.value.kind == "invalid_arguments"
    assert exc_info.value.retryable is False
    assert exc_info.value.call_id == "stable-call"
    assert exc_info.value.__cause__ is None
    assert requests == []


@pytest.mark.parametrize("container_kind", ["mapping", "sequence"])
def test_script_sdk_rejects_cyclic_arguments_before_post(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    container_kind: str,
) -> None:
    """Cyclic arguments must use the stable local rejection instead of escaping as recursion errors."""
    _configure(monkeypatch, tmp_path)
    requests: list[Request] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        requests.append(request)
        return io.BytesIO(b"{}")

    if container_kind == "mapping":
        cyclic_mapping: dict[str, object] = {}
        cyclic_mapping["self"] = cyclic_mapping
        cyclic: object = cyclic_mapping
    else:
        cyclic_sequence: list[object] = []
        cyclic_sequence.append(cyclic_sequence)
        cyclic = cyclic_sequence

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0.0001).call("website", "read_url", payload=cyclic)

    assert exc_info.value.kind == "invalid_arguments"
    assert exc_info.value.retryable is False
    assert exc_info.value.call_id == "stable-call"
    assert exc_info.value.__cause__ is None
    assert requests == []


def _http_error(request: Request, status: int, reason: str, detail: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(request.full_url, status, reason, {}, io.BytesIO(detail))


@pytest.mark.parametrize(
    "first_failure",
    [
        pytest.param(lambda _request: urllib.error.URLError("connection reset"), id="lost-before-dispatch"),
        pytest.param(
            lambda _request: urllib.error.URLError("connection reset after acceptance"),
            id="accepted-response-lost",
        ),
        pytest.param(
            lambda request: _http_error(request, 503, "Service Unavailable", b'{"detail":"temporarily unavailable"}'),
            id="owner-runtime-restarting",
        ),
        pytest.param(
            lambda request: _http_error(request, 429, "Too Many Requests", b'{"detail":"rate limited"}'),
            id="rate-limited",
        ),
    ],
)
def test_script_sdk_retries_same_submit_after_a_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first_failure: Callable[[Request], Exception],
) -> None:
    """A lost POST, a lost response, a restarting owner runtime, or a 429 replays the same stable call."""
    _configure(monkeypatch, tmp_path)
    methods: list[str] = []
    payloads: list[bytes | None] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        methods.append(request.method)
        payloads.append(request.data)
        if len(methods) == 1:
            raise first_failure(request)
        return io.BytesIO(_receipt("completed", result="accepted earlier"))

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    result = MindRoomTools(poll_interval_seconds=0.0001).call("website", "read_url", url="https://example.org/")

    assert result == "accepted earlier"
    assert methods == ["POST", "POST"]
    assert payloads[0] == payloads[1]


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
    """Replaying an ambiguous submit cannot consume a different call identity."""
    _configure(monkeypatch, tmp_path)
    methods: list[str] = []

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        methods.append(request.method)
        if len(methods) == 1:
            raise _http_error(request, 503, "Service Unavailable", b'{"detail":"acceptance not yet determined"}')
        return io.BytesIO(
            _receipt(
                "completed",
                result="old result",
                arguments=receipt_arguments,
                toolkit_name=receipt_toolkit,
                function_name=receipt_function,
            ),
        )

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0.0001).call(
            "website",
            "read_url",
            url="https://example.org/",
        )

    assert exc_info.value.kind == "stable_call_conflict"
    assert exc_info.value.retryable is False
    assert methods == ["POST", "POST"]


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

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0.0001).call("website", "read_url", url="https://example.org/")

    assert exc_info.value.kind == "capability_revoked"
    assert exc_info.value.retryable is False
    assert exc_info.value.call_id == "stable-call"


def test_script_sdk_raises_typed_approval_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A denied background call is distinguishable from a successful string result."""
    _configure(monkeypatch, tmp_path)

    def urlopen(_request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        return io.BytesIO(
            _receipt(
                "failed",
                error={"kind": "approval_denied", "message": "Not this time.", "retryable": False},
                arguments={},
            ),
        )

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0.0001).call("website", "read_url")

    assert exc_info.value.kind == "approval_denied"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_receipt("declined"), id="removed-state-declined"),
        pytest.param(_receipt("cancelled"), id="removed-state-cancelled"),
        pytest.param(_receipt([]), id="unhashable-state-list"),
        pytest.param(_receipt({}), id="unhashable-state-mapping"),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(b"not json", id="not-json"),
    ],
)
def test_script_sdk_rejects_invalid_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes,
) -> None:
    """Removed call states, malformed states, and non-receipt bodies become one stable non-retryable SDK error."""
    _configure(monkeypatch, tmp_path)

    def urlopen(_request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        return io.BytesIO(body)

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    with pytest.raises(MindRoomToolCallError) as exc_info:
        MindRoomTools(poll_interval_seconds=0.0001).call("website", "read_url", url="https://example.org/")

    assert exc_info.value.kind == "invalid_response"
    assert exc_info.value.retryable is False
    assert exc_info.value.call_id == "stable-call"


def test_script_sdk_refuses_an_oversized_capability_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A capability file over the token limit is refused before any gateway request."""
    _configure(monkeypatch, tmp_path)
    (tmp_path / "capability").write_text("x" * (MAX_TOKEN_BYTES + 1), encoding="utf-8")

    with pytest.raises(RuntimeError, match="capability token is invalid"):
        MindRoomTools()

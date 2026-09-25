"""Native desktop protocol contract tests."""

# ruff: noqa: D103

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from mindroom.desktop.native_protocol import (
    MAX_NATIVE_INPUT_BYTES,
    NativeProtocolError,
    encode_native_message,
    parse_native_request,
)


def _request(**updates: object) -> bytes:
    payload: dict[str, object] = {
        "v": 1,
        "request_id": str(uuid4()),
        "action": "status",
        "parameters": {},
    }
    payload.update(updates)
    return json.dumps(payload).encode()


def test_parse_native_request_accepts_strict_record() -> None:
    request_id = str(uuid4())
    request = parse_native_request(_request(request_id=request_id))
    assert (request.request_id, request.action, request.parameters) == (request_id, "status", {})


def test_parse_native_request_accepts_app_selection_update() -> None:
    request = parse_native_request(
        _request(action="set_allowed_apps", parameters={"expected_revision": 1, "allowed_app_ids": []}),
    )
    assert request.action == "set_allowed_apps"


@pytest.mark.parametrize("action", ["set_local_access", "decide_shell", "grant_shell", "revoke_shell"])
def test_parse_native_request_accepts_local_access_actions(action: str) -> None:
    assert parse_native_request(_request(action=action)).action == action


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b"{", "invalid_json"),
        (b"[]", "invalid_request"),
        (_request(v=2), "invalid_request"),
        (_request(v=True), "invalid_request"),
        (_request(request_id="not-a-uuid"), "invalid_request"),
        (_request(action="shell"), "invalid_request"),
        (_request(parameters=[]), "invalid_request"),
        (_request(extra=True), "invalid_request"),
    ],
    ids=[
        "invalid-json",
        "non-object",
        "unsupported-version",
        "boolean-version",
        "invalid-request-id",
        "unknown-action",
        "non-object-parameters",
        "extra-key",
    ],
)
def test_parse_native_request_rejects_invalid_records(line: bytes, code: str) -> None:
    with pytest.raises(NativeProtocolError) as caught:
        parse_native_request(line)
    assert caught.value.code == code


def test_parse_native_request_rejects_oversize_before_decoding() -> None:
    with pytest.raises(NativeProtocolError, match="65,536") as caught:
        parse_native_request(b"{" + b" " * MAX_NATIVE_INPUT_BYTES)
    assert caught.value.code == "request_too_large"


def test_encode_native_message_is_bounded_ndjson() -> None:
    encoded = encode_native_message({"v": 1, "type": "status", "sequence": 1, "status": {}})
    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == {"v": 1, "type": "status", "sequence": 1, "status": {}}


def test_protocol_error_payload_is_stable_and_redacted() -> None:
    error = NativeProtocolError("invalid_request", "Bad value.", recovery="Try again.", retryable=True)
    assert error.to_payload() == {
        "code": "invalid_request",
        "message": "Bad value.",
        "recovery": "Try again.",
        "retryable": True,
    }

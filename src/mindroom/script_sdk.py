"""Stdlib-only client available inside background Python script processes."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_GATEWAY_URL_ENV = "MINDROOM_SCRIPT_GATEWAY_URL"
_RUN_ID_ENV = "MINDROOM_SCRIPT_RUN_ID"
_TOKEN_PATH_ENV = "MINDROOM_SCRIPT_TOKEN_PATH"  # noqa: S105 - this names a path, not a token.
_DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_MAX_TOKEN_BYTES = 4096
_TRANSPORT_ERROR = "The background tool gateway transport failed after dispatch may have been accepted."
_INVALID_RECEIPT_ERROR = "The background tool gateway returned an invalid receipt."
_CONFLICTING_RECEIPT_ERROR = "The stable call ID belongs to a different tool request."
_INVALID_ARGUMENTS_ERROR = "Background tool arguments must have an unambiguous JSON representation."
_CAPABILITY_UNAVAILABLE_MESSAGE = "The MindRoom script capability token is unavailable."
_CAPABILITY_INVALID_MESSAGE = "The MindRoom script capability token is invalid."


class MindRoomToolCallError(RuntimeError):
    """Stable framework or transport failure returned by the script gateway."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retryable: bool,
        call_id: str,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.call_id = call_id

    @classmethod
    def from_receipt(cls, receipt: _Receipt) -> MindRoomToolCallError:
        """Build one typed exception from a terminal non-success receipt."""
        error = cast("dict[str, object]", receipt.error) if isinstance(receipt.error, dict) else {}
        message = error.get("message")
        kind = error.get("kind")
        retryable = error.get("retryable")
        return cls(
            str(message or f"Background tool call ended in state {receipt.state}."),
            kind=str(kind or receipt.state),
            retryable=retryable is True,
            call_id=receipt.call_id,
        )


@dataclass(frozen=True, slots=True)
class _Receipt:
    run_id: str
    call_id: str
    toolkit_name: str
    function_name: str
    arguments_digest: str
    state: str
    result: object | None
    error: object | None


class MindRoomTools:
    """Blocking governed-tool client for one launcher-injected script run."""

    def __init__(
        self,
        *,
        http_timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._gateway_url = _required_env(_GATEWAY_URL_ENV).rstrip("/")
        parsed_gateway = urllib.parse.urlsplit(self._gateway_url)
        if parsed_gateway.scheme not in {"http", "https"} or not parsed_gateway.netloc:
            msg = "MINDROOM_SCRIPT_GATEWAY_URL must be an HTTP(S) URL."
            raise RuntimeError(msg)
        self._run_id = _required_env(_RUN_ID_ENV)
        self._token = _read_token(Path(_required_env(_TOKEN_PATH_ENV)))
        self._http_timeout_seconds = http_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    def call(self, toolkit_name: str, function_name: str, **arguments: object) -> object:
        """Execute one logical governed call and poll only its stable call ID."""
        call_id = uuid.uuid4().hex
        wire_arguments = _json_wire_arguments(arguments, call_id=call_id)
        arguments_digest = _digest_arguments(wire_arguments)
        try:
            receipt = self._submit(
                call_id,
                toolkit_name,
                function_name,
                wire_arguments,
                arguments_digest=arguments_digest,
            )
        except MindRoomToolCallError as exc:
            if not exc.retryable:
                raise
            receipt = None

        while receipt is None or receipt.state == "pending":
            if self._poll_interval_seconds > 0:
                time.sleep(self._poll_interval_seconds)
            try:
                receipt = self._poll(
                    call_id,
                    toolkit_name=toolkit_name,
                    function_name=function_name,
                    arguments_digest=arguments_digest,
                )
            except MindRoomToolCallError as exc:
                if not exc.retryable:
                    raise
                receipt = None

        if receipt.state == "completed":
            return receipt.result
        raise MindRoomToolCallError.from_receipt(receipt)

    def _submit(
        self,
        call_id: str,
        toolkit_name: str,
        function_name: str,
        arguments: dict[str, object],
        *,
        arguments_digest: str,
    ) -> _Receipt:
        payload = {
            "run_id": self._run_id,
            "call_id": call_id,
            "toolkit_name": toolkit_name,
            "function_name": function_name,
            "arguments": arguments,
        }
        return self._request(
            urllib.request.Request(  # noqa: S310 - the constructor restricts the gateway to HTTP(S).
                f"{self._gateway_url}/calls",
                data=json.dumps(payload, allow_nan=False, separators=(",", ":")).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
                method="POST",
            ),
            call_id=call_id,
            toolkit_name=toolkit_name,
            function_name=function_name,
            arguments_digest=arguments_digest,
        )

    def _poll(
        self,
        call_id: str,
        *,
        toolkit_name: str,
        function_name: str,
        arguments_digest: str,
    ) -> _Receipt:
        run_id = urllib.parse.quote(self._run_id, safe="")
        encoded_call_id = urllib.parse.quote(call_id, safe="")
        return self._request(
            urllib.request.Request(  # noqa: S310 - the constructor restricts the gateway to HTTP(S).
                f"{self._gateway_url}/runs/{run_id}/calls/{encoded_call_id}",
                headers={"Authorization": f"Bearer {self._token}"},
                method="GET",
            ),
            call_id=call_id,
            toolkit_name=toolkit_name,
            function_name=function_name,
            arguments_digest=arguments_digest,
        )

    def _request(
        self,
        request: urllib.request.Request,
        *,
        call_id: str,
        toolkit_name: str,
        function_name: str,
        arguments_digest: str,
    ) -> _Receipt:
        try:
            with urllib.request.urlopen(request, timeout=self._http_timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, call_id=call_id) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MindRoomToolCallError(
                _TRANSPORT_ERROR,
                kind="transport",
                retryable=True,
                call_id=call_id,
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MindRoomToolCallError(
                _INVALID_RECEIPT_ERROR,
                kind="invalid_response",
                retryable=False,
                call_id=call_id,
            ) from exc
        return _parse_receipt(
            payload,
            expected_run_id=self._run_id,
            expected_call_id=call_id,
            expected_toolkit_name=toolkit_name,
            expected_function_name=function_name,
            expected_arguments_digest=arguments_digest,
        )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required inside a MindRoom background script."
        raise RuntimeError(msg)
    return value


def _read_token(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(_CAPABILITY_UNAVAILABLE_MESSAGE)
    if path.stat().st_size > _MAX_TOKEN_BYTES:
        raise RuntimeError(_CAPABILITY_INVALID_MESSAGE)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(_CAPABILITY_INVALID_MESSAGE)
    return token


def _digest_arguments(arguments: dict[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_wire_arguments(arguments: dict[str, object], *, call_id: str) -> dict[str, object]:
    try:
        _reject_mixed_mapping_key_types(arguments)
        encoded = json.dumps(
            arguments,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        wire_arguments = json.loads(encoded)
    except (RecursionError, TypeError, ValueError):
        raise MindRoomToolCallError(
            _INVALID_ARGUMENTS_ERROR,
            kind="invalid_arguments",
            retryable=False,
            call_id=call_id,
        ) from None
    return cast("dict[str, object]", wire_arguments)


def _reject_mixed_mapping_key_types(value: object) -> None:
    if isinstance(value, dict):
        if len({type(key) for key in value}) > 1:
            raise TypeError(_INVALID_ARGUMENTS_ERROR)
        for item in value.values():
            _reject_mixed_mapping_key_types(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_mixed_mapping_key_types(item)


def _parse_receipt(
    value: object,
    *,
    expected_run_id: str,
    expected_call_id: str,
    expected_toolkit_name: str,
    expected_function_name: str,
    expected_arguments_digest: str,
) -> _Receipt:
    if not isinstance(value, dict):
        raise MindRoomToolCallError(
            _INVALID_RECEIPT_ERROR,
            kind="invalid_response",
            retryable=False,
            call_id=expected_call_id,
        )
    payload = cast("dict[str, object]", value)
    run_id = payload.get("run_id")
    call_id = payload.get("call_id")
    toolkit_name = payload.get("toolkit_name")
    function_name = payload.get("function_name")
    arguments_digest = payload.get("arguments_digest")
    state = payload.get("state")
    if (
        run_id != expected_run_id
        or call_id != expected_call_id
        or state
        not in {
            "pending",
            "completed",
            "failed",
            "indeterminate",
        }
    ):
        raise MindRoomToolCallError(
            _INVALID_RECEIPT_ERROR,
            kind="invalid_response",
            retryable=False,
            call_id=expected_call_id,
        )
    if (
        toolkit_name != expected_toolkit_name
        or function_name != expected_function_name
        or arguments_digest != expected_arguments_digest
    ):
        raise MindRoomToolCallError(
            _CONFLICTING_RECEIPT_ERROR,
            kind="stable_call_conflict",
            retryable=False,
            call_id=expected_call_id,
        )
    return _Receipt(
        run_id=expected_run_id,
        call_id=expected_call_id,
        toolkit_name=expected_toolkit_name,
        function_name=expected_function_name,
        arguments_digest=expected_arguments_digest,
        state=cast("str", state),
        result=payload.get("result"),
        error=payload.get("error"),
    )


def _http_error(exc: urllib.error.HTTPError, *, call_id: str) -> MindRoomToolCallError:
    retryable = exc.code >= 500
    try:
        payload = json.loads(exc.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return MindRoomToolCallError(
        str(detail or f"Background tool gateway request failed with status {exc.code}."),
        kind="gateway_unavailable" if retryable else "request_rejected",
        retryable=retryable,
        call_id=call_id,
    )


_PUBLIC_SDK = (MindRoomToolCallError, MindRoomTools)

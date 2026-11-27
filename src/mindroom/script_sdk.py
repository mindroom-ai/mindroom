"""Stdlib-only client available inside background Python script processes."""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import cast

from mindroom.script_runs.launch_contract import digest_arguments
from mindroom.script_runs.transport import (
    GatewayDecodeError,
    GatewayHTTPError,
    GatewayTransportError,
    ScriptGatewayTransport,
)

__all__ = ["MindRoomToolCallError", "MindRoomTools"]

_DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_TRANSPORT_ERROR = "The background tool gateway transport failed after dispatch may have been accepted."
_INVALID_RECEIPT_ERROR = "The background tool gateway returned an invalid receipt."
_CONFLICTING_RECEIPT_ERROR = "The stable call ID belongs to a different tool request."
_INVALID_ARGUMENTS_ERROR = "Background tool arguments must have an unambiguous JSON representation."


class MindRoomToolCallError(RuntimeError):
    """Stable framework or transport failure returned by the script gateway.

    `state` is the terminal receipt's state, `failed` or `indeterminate`; `rejected` when the arguments or every
    submission were refused, so the call never ran; or None when the call may have been accepted but no receipt
    settled it.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retryable: bool,
        call_id: str,
        state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.call_id = call_id
        self.state = state

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
            state=receipt.state,
        )


@dataclass(frozen=True, slots=True)
class _Receipt:
    call_id: str
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
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            msg = "poll_interval_seconds must be positive."
            raise ValueError(msg)
        self._transport = ScriptGatewayTransport(timeout_seconds=http_timeout_seconds)
        self._poll_interval_seconds = poll_interval_seconds

    def call(self, toolkit_name: str, function_name: str, /, **arguments: object) -> object:
        """Execute one logical governed call and poll only its stable call ID."""
        call_id = uuid.uuid4().hex
        wire_arguments = _json_wire_arguments(arguments, call_id=call_id)
        arguments_digest = digest_arguments(wire_arguments)
        receipt: _Receipt | None = None
        # Only a 4xx answer proves the gateway did not record a submission.
        maybe_accepted = False
        while receipt is None:
            try:
                receipt = self._submit(
                    call_id,
                    toolkit_name,
                    function_name,
                    wire_arguments,
                    arguments_digest=arguments_digest,
                )
            except MindRoomToolCallError as exc:
                maybe_accepted = maybe_accepted or exc.kind not in {"request_rejected", "rate_limited"}
                if not exc.retryable:
                    if not maybe_accepted:
                        exc.state = "rejected"
                    raise
                time.sleep(self._poll_interval_seconds)

        while receipt.state == "pending":
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
            "run_id": self._transport.run_id,
            "call_id": call_id,
            "toolkit_name": toolkit_name,
            "function_name": function_name,
            "arguments": arguments,
        }
        return self._request(
            "POST",
            "calls",
            payload,
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
        run_id = urllib.parse.quote(self._transport.run_id, safe="")
        encoded_call_id = urllib.parse.quote(call_id, safe="")
        return self._request(
            "GET",
            f"runs/{run_id}/calls/{encoded_call_id}",
            call_id=call_id,
            toolkit_name=toolkit_name,
            function_name=function_name,
            arguments_digest=arguments_digest,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        call_id: str,
        toolkit_name: str,
        function_name: str,
        arguments_digest: str,
    ) -> _Receipt:
        try:
            payload = self._transport.request_json(method, path, body)
        except GatewayHTTPError as exc:
            raise _http_error(exc, call_id=call_id) from exc
        except GatewayTransportError as exc:
            raise MindRoomToolCallError(
                _TRANSPORT_ERROR,
                kind="transport",
                retryable=True,
                call_id=call_id,
            ) from exc
        except GatewayDecodeError as exc:
            raise _invalid_receipt(call_id) from exc
        return _parse_receipt(
            payload,
            expected_run_id=self._transport.run_id,
            expected_call_id=call_id,
            expected_toolkit_name=toolkit_name,
            expected_function_name=function_name,
            expected_arguments_digest=arguments_digest,
        )


def _json_wire_arguments(arguments: dict[str, object], *, call_id: str) -> dict[str, object]:
    try:
        _reject_mixed_mapping_key_types(arguments)
        encoded = json.dumps(
            arguments,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        wire_arguments = json.loads(encoded)
    except (RecursionError, TypeError, ValueError):
        raise MindRoomToolCallError(
            _INVALID_ARGUMENTS_ERROR,
            kind="invalid_arguments",
            retryable=False,
            call_id=call_id,
            # Raised before any submission, so the call never ran.
            state="rejected",
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
        raise _invalid_receipt(expected_call_id)
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
        or not isinstance(state, str)
        or state
        not in {
            "pending",
            "completed",
            "failed",
            "indeterminate",
        }
    ):
        raise _invalid_receipt(expected_call_id)
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
        call_id=expected_call_id,
        state=state,
        result=payload.get("result"),
        error=payload.get("error"),
    )


def _invalid_receipt(call_id: str) -> MindRoomToolCallError:
    return MindRoomToolCallError(
        _INVALID_RECEIPT_ERROR,
        kind="invalid_response",
        retryable=False,
        call_id=call_id,
    )


def _http_error(exc: GatewayHTTPError, *, call_id: str) -> MindRoomToolCallError:
    if exc.status == 429:
        kind, retryable = "rate_limited", True
    elif exc.status >= 500:
        kind, retryable = "gateway_unavailable", True
    elif 400 <= exc.status < 500:
        kind, retryable = "request_rejected", False
    else:
        # Blocked 3xx redirect; leave state indeterminate rather than rejected.
        kind, retryable = "unexpected_redirect", False
    return MindRoomToolCallError(
        exc.detail or f"Background tool gateway request failed with status {exc.status}.",
        kind=kind,
        retryable=retryable,
        call_id=call_id,
    )

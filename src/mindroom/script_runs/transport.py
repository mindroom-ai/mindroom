"""Stdlib-only HTTP/auth transport shared by the script and workflow SDKs."""

from __future__ import annotations

import json
import os
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mindroom.script_runs.launch_contract import GATEWAY_URL_ENV, MAX_TOKEN_BYTES, RUN_ID_ENV, TOKEN_PATH_ENV

__all__ = ["GatewayDecodeError", "GatewayHTTPError", "GatewayTransportError", "ScriptGatewayTransport"]

_CAPABILITY_UNAVAILABLE_MESSAGE = "The MindRoom script capability token is unavailable."
_CAPABILITY_INVALID_MESSAGE = "The MindRoom script capability token is invalid."
_TRANSPORT_ERROR_MESSAGE = "The script gateway transport failed."
_DECODE_ERROR_MESSAGE = "The script gateway returned invalid JSON."


class GatewayHTTPError(RuntimeError):
    """Non-2xx HTTP response returned by the script gateway."""

    def __init__(
        self,
        *,
        status: int,
        reason: str,
        detail: str | None,
        payload: dict[str, object] | None,
        sent_bytes: int,
    ) -> None:
        super().__init__(detail or f"Script gateway request failed with status {status}.")
        self.status = status
        self.reason = reason
        self.detail = detail
        self.payload = payload
        self.sent_bytes = sent_bytes


class GatewayTransportError(RuntimeError):
    """Network, timeout, or HTTP protocol error talking to the script gateway."""


class GatewayDecodeError(RuntimeError):
    """Invalid UTF-8 or JSON payload in a 2xx script gateway response."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl


class ScriptGatewayTransport:
    """Authenticated, proxy-bypassing, no-redirect JSON transport for script gateway calls."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self._gateway_url = _validated_gateway_url(_required_env(GATEWAY_URL_ENV))
        self._run_id = _required_env(RUN_ID_ENV)
        self._token = _read_token(Path(_required_env(TOKEN_PATH_ENV)))
        self._timeout_seconds = timeout_seconds
        # Scripts may inherit proxy env vars; never route bearer tokens through a proxy or redirect.
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    @property
    def gateway_url(self) -> str:
        """Return the validated gateway base URL without a trailing slash."""
        return self._gateway_url

    @property
    def run_id(self) -> str:
        """Return the current script run identifier."""
        return self._run_id

    def request_json(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> object:
        """Send one authenticated JSON request to *path* and return the decoded JSON body."""
        data = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
        sent_bytes = len(data or b"")
        request_headers = {
            "Authorization": f"Bearer {self._token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
            **(headers or {}),
        }
        request = Request(  # noqa: S310 - the constructor restricts the gateway to HTTP(S).
            f"{self.gateway_url}/{path.lstrip('/')}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with self._open(request) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            raise _http_error(exc, sent_bytes=sent_bytes) from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise GatewayTransportError(_TRANSPORT_ERROR_MESSAGE) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayDecodeError(_DECODE_ERROR_MESSAGE) from exc

    def _open(self, request: Request) -> HTTPResponse:
        return self._opener.open(request, timeout=self._timeout_seconds)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required inside a MindRoom background script."
        raise RuntimeError(msg)
    return value


def _validated_gateway_url(raw_url: str) -> str:
    invalid = f"{GATEWAY_URL_ENV} must be an HTTP(S) URL."
    try:
        parsed = urlsplit(raw_url)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(invalid) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in raw_url
        or "#" in raw_url
        or any(ch.isspace() for ch in raw_url)
    ):
        raise RuntimeError(invalid)
    return raw_url.rstrip("/")


def _read_token(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(_CAPABILITY_UNAVAILABLE_MESSAGE)
    if path.stat().st_size > MAX_TOKEN_BYTES:
        raise RuntimeError(_CAPABILITY_INVALID_MESSAGE)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError(_CAPABILITY_INVALID_MESSAGE) from None
    if not token or not token.isascii() or any(ch.isspace() for ch in token):
        raise RuntimeError(_CAPABILITY_INVALID_MESSAGE)
    return token


def _http_error(exc: HTTPError, *, sent_bytes: int) -> GatewayHTTPError:
    with exc:
        try:
            parsed = json.loads(exc.read())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, HTTPException):
            parsed = None
    payload = cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None
    raw_detail = payload.get("detail") if payload is not None else None
    return GatewayHTTPError(
        status=exc.code,
        reason=str(exc.reason),
        detail=str(raw_detail) if raw_detail else None,
        payload=payload,
        sent_bytes=sent_bytes,
    )

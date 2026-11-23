"""Small stdlib HTTP client using only server-injected CLI authority."""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import TYPE_CHECKING, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json, read_json
from mindroom.agent_cli.shell_contract import (
    AGENT_CLI_TOKEN_ENV,
    AGENT_CLI_URL_ENV,
    AGENT_CLI_WINDOW_ENV,
    AGENT_CLI_WINDOW_HEADER,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from http.client import HTTPResponse

_HTTP_TIMEOUT_SECONDS = 30
_MAX_LONG_POLL_SECONDS = 25
_CANCEL_TIMEOUT_SECONDS = 8


class AgentCliUnavailableError(RuntimeError):
    """Authority or transport could not yield a known response."""


class AgentCliRefusedError(AgentCliUnavailableError):
    """The server answered a request with a 4xx it gave no reason for, so it did not act on it."""


def _rejection(error: HTTPError) -> ValueError:
    """Relay the server's bounded ``detail`` so the caller learns why it was refused."""
    msg = "Agent CLI request was rejected"
    try:
        body = read_json(error.read(MAX_ENVELOPE_BYTES + 1))
    except (OSError, HTTPException, ValueError):
        body = None
    match body:
        case {"detail": str(detail)} if detail:
            return ValueError(f"{msg}: {detail}")
        case _:
            return ValueError(msg)


def _envelope(payload: bytes) -> dict[str, object]:
    result = read_json(payload)
    if not isinstance(result, dict):
        msg = "Agent CLI returned an invalid envelope"
        raise AgentCliUnavailableError(msg)
    return cast("dict[str, object]", result)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl


class AgentCliClient:
    """Use the response's grant from the environment; never print it or follow redirects."""

    def __init__(self) -> None:
        self._url = os.environ.get(AGENT_CLI_URL_ENV, "").rstrip("/")
        self._token = os.environ.get(AGENT_CLI_TOKEN_ENV, "")
        self._window = os.environ.get(AGENT_CLI_WINDOW_ENV)
        parsed = urlsplit(self._url)
        try:
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path
                or not self._token
                or len(self._token) > 4096
                or not self._token.isascii()
                or any(character.isspace() for character in self._token)
            ):
                raise ValueError  # noqa: TRY301 - Normalize configuration errors without exposing token data.
        except ValueError as exc:
            msg = "Agent CLI authority is unavailable"
            raise AgentCliUnavailableError(msg) from exc
        # The CLI only calls its own MindRoom API; never send the grant through an environment proxy.
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())
        # The one connection a command's stdin goes over, straight to that API and following no redirect.
        connection = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        self._stdin = connection(parsed.netloc, timeout=_HTTP_TIMEOUT_SECONDS)

    def operation(self, payload: dict[str, object]) -> dict[str, object]:
        """Submit once; callers retain the exact call ID on uncertain acceptance."""
        return self._request("/api/agent-cli/operations", canonical_json(payload).encode("utf-8"))

    def receipt(
        self,
        call_id: str,
        *,
        timeout: float = _HTTP_TIMEOUT_SECONDS,
        wait: float | None = None,
        after_progress: int | None = None,
    ) -> dict[str, object]:
        """Read one canonical call ID, optionally long-polling up to 25 seconds."""
        route = f"/api/agent-cli/calls/{UUID(call_id)}"
        params: list[str] = []
        if wait is not None and wait > 0:
            params.append(f"wait={min(wait, _MAX_LONG_POLL_SECONDS):g}")
        if after_progress is not None and after_progress >= 0:
            params.append(f"after_progress={after_progress}")
        if params:
            route = f"{route}?{'&'.join(params)}"
        return self._request(route, timeout=min(timeout, _HTTP_TIMEOUT_SECONDS))

    def cancel(self, call_id: str, *, timeout: float = _CANCEL_TIMEOUT_SECONDS) -> dict[str, object]:
        """Idempotently cancel one canonical call ID."""
        return self._request(
            f"/api/agent-cli/calls/{UUID(call_id)}/cancel",
            b"",
            timeout=min(timeout, _HTTP_TIMEOUT_SECONDS),
        )

    def stdin(self, call_id: str, seq: int, data: bytes, *, eof: bool) -> dict[str, object]:
        """Write chunk ``seq`` to a streamed command's stdin, once however often it is sent; ValueError refuses it."""
        chunk = {"seq": seq, "b": base64.b64encode(data).decode("ascii"), "eof": eof}
        route, body = f"/api/agent-cli/calls/{UUID(call_id)}/stdin", canonical_json(chunk).encode("utf-8")
        try:
            # The chunks share a connection; one the server closed is opened again when this chunk is sent again.
            self._stdin.request("POST", route, body, self._headers())
            response = self._stdin.getresponse()
            payload = response.read(MAX_ENVELOPE_BYTES + 1)
        except (OSError, HTTPException) as exc:
            self._stdin.close()
            msg = f"Agent CLI transport outcome is unknown; check that this shell can reach {self._url}"
            raise AgentCliUnavailableError(msg) from exc
        if response.status != HTTPStatus.OK:
            # A 408, 429 or 5xx passes, so the chunk is sent again; any other answer is the same next time.
            passing = response.status in {408, 429} or response.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            msg = f"HTTP {response.status}"
            raise (AgentCliUnavailableError if passing else ValueError)(msg)
        return _envelope(payload)

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        if self._window:
            headers[AGENT_CLI_WINDOW_HEADER] = self._window
        return headers

    def stream(self, call_id: str) -> Iterator[dict[str, object]]:
        """Yield a streamed call's frames as they arrive: one JSON object per line, the last one its receipt."""
        with self._response(f"/api/agent-cli/calls/{UUID(call_id)}/stream") as response:
            while line := response.readline(MAX_ENVELOPE_BYTES + 2):
                yield _envelope(line.rstrip(b"\n"))

    def _request(
        self,
        route: str,
        data: bytes | None = None,
        *,
        timeout: float = _HTTP_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        with self._response(route, data, timeout=timeout) as response:
            return _envelope(response.read(MAX_ENVELOPE_BYTES + 1))

    @contextmanager
    def _response(
        self,
        route: str,
        data: bytes | None = None,
        *,
        timeout: float = _HTTP_TIMEOUT_SECONDS,
    ) -> Iterator[HTTPResponse]:
        """Open one request and report whatever fails while its response is read as the CLI's own errors."""
        request = Request(  # noqa: S310 - HTTP(S) origin validated at construction.
            self._url + route,
            data=data,
            headers=self._headers(),
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                yield response
        except HTTPError as exc:
            if exc.code in {400, 409, 413, 422}:
                raise _rejection(exc) from None
            msg = "Agent CLI authority or transport is unavailable"
            refused = HTTPStatus.BAD_REQUEST <= exc.code < HTTPStatus.INTERNAL_SERVER_ERROR
            raise (AgentCliRefusedError if refused else AgentCliUnavailableError)(msg) from None
        except (OSError, HTTPException, URLError, ValueError) as exc:
            # Name the address so a blocked route back to MindRoom is diagnosable from the shell.
            msg = f"Agent CLI transport outcome is unknown; check that this shell can reach {self._url}"
            raise AgentCliUnavailableError(msg) from exc

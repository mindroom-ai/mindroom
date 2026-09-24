"""Small stdlib HTTP client using only server-injected CLI authority."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json, read_json


class AgentCliUnavailableError(RuntimeError):
    """Authority or transport could not yield a known response."""


def _rejection(error: HTTPError) -> ValueError:
    """Relay the server's bounded ``detail`` so the caller learns why it was refused."""
    msg = "Agent CLI request was rejected"
    try:
        body = read_json(error.read(MAX_ENVELOPE_BYTES + 1))
    except (OSError, ValueError):
        body = None
    match body:
        case {"detail": str(detail)} if detail:
            return ValueError(f"{msg}: {detail}")
        case _:
            return ValueError(msg)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl


class AgentCliClient:
    """Use the private token file; never print its contents or follow redirects."""

    def __init__(self) -> None:
        self._url = os.environ.get("MINDROOM_AGENT_CLI_GATEWAY_URL", "").rstrip("/")
        token_path = os.environ.get("MINDROOM_AGENT_CLI_TOKEN_PATH", "")
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
                or not token_path
            ):
                raise ValueError  # noqa: TRY301 - Normalize configuration errors without exposing token data.
            with Path(token_path).open("rb") as stream:
                token = stream.read(4097)
            self._token = token.decode("ascii").strip()
            if not self._token or len(token) > 4096 or any(character.isspace() for character in self._token):
                raise ValueError  # noqa: TRY301 - Normalize configuration errors without exposing token data.
        except (OSError, ValueError) as exc:
            msg = "Agent CLI authority is unavailable"
            raise AgentCliUnavailableError(msg) from exc
        self._opener = build_opener(_NoRedirects())

    def operation(self, payload: dict[str, object]) -> dict[str, object]:
        """Submit once; callers retain the exact call ID on uncertain acceptance."""
        return self._request("/api/agent-cli/operations", canonical_json(payload).encode("utf-8"))

    def receipt(self, call_id: str) -> dict[str, object]:
        """Read one canonical call ID without client-side identity selectors."""
        return self._request(f"/api/agent-cli/calls/{UUID(call_id)}")

    def _request(self, route: str, data: bytes | None = None) -> dict[str, object]:
        request = Request(  # noqa: S310 - HTTP(S) origin validated at construction.
            self._url + route,
            data=data,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                result = read_json(response.read(MAX_ENVELOPE_BYTES + 1))
        except HTTPError as exc:
            if exc.code in {400, 409, 413, 422}:
                raise _rejection(exc) from None
            msg = "Agent CLI authority or transport is unavailable"
            raise AgentCliUnavailableError(msg) from None
        except (OSError, URLError, ValueError) as exc:
            msg = "Agent CLI transport outcome is unknown"
            raise AgentCliUnavailableError(msg) from exc
        if not isinstance(result, dict):
            msg = "Agent CLI returned an invalid envelope"
            raise AgentCliUnavailableError(msg)
        return cast("dict[str, object]", result)

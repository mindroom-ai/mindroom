"""Trusted-upstream auth fetches its JWKS over real HTTP: rotated keys work at once, and redirects are refused."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from mindroom.api import auth
from tests.api.conftest import trusted_upstream_jwks, trusted_upstream_jwt, trusted_upstream_jwt_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jwt import PyJWKClient


class _JwksServer:
    def __init__(self) -> None:
        self.jwks: dict[str, Any] = {"keys": []}
        self.fetches = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.partition("?")[0] == "/moved":
                    self.send_response(302)
                    self.send_header("Location", "/jwks")
                    self.end_headers()
                    return
                server.fetches += 1
                body = json.dumps(server.jwks).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                """Keep the request log out of the test output."""

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def jwks_server() -> Iterator[_JwksServer]:
    """Serve a mutable JWKS at `/jwks` and a redirect to it at `/moved`."""
    server = _JwksServer()
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _client(jwks_url: str) -> PyJWKClient:
    client = auth._build_trusted_upstream_jwt_client(auth._TrustedUpstreamAuthSettings(enabled=True, jwks_url=jwks_url))
    assert client is not None
    return client


def _verify(client: PyJWKClient, token: str) -> dict[str, Any]:
    request = SimpleNamespace(headers={"X-Trusted-Jwt": token})
    return asyncio.run(
        auth._verified_trusted_upstream_jwt_claims(
            request,
            client,
            header="X-Trusted-Jwt",
            audience="mindroom-dashboard",
            issuer="https://issuer.example",
            required_claims=("exp", "iat"),
            invalid_detail="Invalid trusted upstream JWT",
        ),
    )


def test_a_rotated_key_verifies_at_once(jwks_server: _JwksServer) -> None:
    """An unknown `kid` refetches the JWKS right away instead of waiting out PyJWT's refresh cooldown."""
    old_key, new_key = trusted_upstream_jwt_key(), trusted_upstream_jwt_key()
    jwks_server.jwks = trusted_upstream_jwks(old_key, kid="old")
    client = _client(f"{jwks_server.base_url}/jwks")
    assert _verify(client, trusted_upstream_jwt(old_key, kid="old"))["sub"] == "user_123"

    jwks_server.jwks = trusted_upstream_jwks(new_key, kid="new")
    assert _verify(client, trusted_upstream_jwt(new_key, kid="new"))["sub"] == "user_123"
    assert jwks_server.fetches == 2


def test_a_redirecting_jwks_url_is_refused_and_named_in_the_log(jwks_server: _JwksServer) -> None:
    """A JWKS URL that redirects fails closed; the log names the setting, asks for the direct URL, and hides the URL."""
    key = trusted_upstream_jwt_key()
    jwks_server.jwks = trusted_upstream_jwks(key)
    moved = f"{jwks_server.base_url}/moved?token=sentinel-jwks-secret"

    with capture_logs() as logs, pytest.raises(HTTPException) as error:
        _verify(_client(moved), trusted_upstream_jwt(key))

    assert error.value.status_code == 401
    assert jwks_server.fetches == 0
    fetch_failures = [entry for entry in logs if entry["event"] == "Trusted upstream JWKS fetch failed"]
    assert fetch_failures == [
        {
            "event": "Trusted upstream JWKS fetch failed",
            "setting": "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL",
            "reason": "redirect refused; configure the direct JWKS URL",
            "log_level": "warning",
        },
    ]
    assert "sentinel-jwks-secret" not in repr(logs)

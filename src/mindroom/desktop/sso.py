"""Browser-assisted Matrix SSO for the local desktop bridge."""

from __future__ import annotations

import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable


_SUCCESS_BODY = b"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>MindRoom sign-in complete</title></head>
<body><h1>Sign-in received</h1><p>Return to the MindRoom terminal. You may close this tab.</p></body>
</html>
"""
_ERROR_BODY = b"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>MindRoom sign-in failed</title></head>
<body><h1>Sign-in failed</h1><p>Return to the MindRoom terminal and retry.</p></body>
</html>
"""


class DesktopSsoError(RuntimeError):
    """Browser-assisted Matrix SSO could not produce one login token."""


class _SsoCallbackServer(HTTPServer):
    """One-process loopback receiver carrying an unguessable callback path."""

    callback_path: str
    login_token: str | None
    callback_error: str | None

    def __init__(self, callback_path: str) -> None:
        self.callback_path = callback_path
        self.login_token = None
        self.callback_error = None
        super().__init__(("127.0.0.1", 0), _SsoCallbackHandler)


class _SsoCallbackHandler(BaseHTTPRequestHandler):
    """Accept exactly one Matrix login token on the generated callback path."""

    server: _SsoCallbackServer

    def do_GET(self) -> None:
        callback = urlsplit(self.path)
        if callback.path != self.server.callback_path:
            self._respond(HTTPStatus.NOT_FOUND, _ERROR_BODY)
            return

        tokens = parse_qs(callback.query, keep_blank_values=True).get("loginToken", [])
        if len(tokens) != 1 or not tokens[0]:
            self.server.callback_error = "Matrix SSO callback did not contain exactly one login token."
            self._respond(HTTPStatus.BAD_REQUEST, _ERROR_BODY)
            return

        self.server.login_token = tokens[0]
        self._respond(HTTPStatus.OK, _SUCCESS_BODY)

    def _respond(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - Base class parameter name.
        """Keep callback URLs and their one-time tokens out of terminal logs."""


def matrix_sso_login_url(
    homeserver: str,
    *,
    redirect_url: str,
    idp_id: str | None = None,
) -> str:
    """Build the standard Matrix SSO redirect URL."""
    provider_path = f"/{quote(idp_id, safe='')}" if idp_id is not None else ""
    return (
        f"{homeserver.rstrip('/')}/_matrix/client/v3/login/sso/redirect{provider_path}?"
        f"{urlencode({'redirectUrl': redirect_url})}"
    )


def receive_sso_login_token(
    homeserver: str,
    *,
    open_browser: bool,
    announce: Callable[[str], None],
    idp_id: str | None = None,
    browser_opener: Callable[[str], bool] | None = None,
) -> str:
    """Run one loopback callback and return the short-lived Matrix login token."""
    callback_path = f"/_mindroom/matrix-sso/{secrets.token_urlsafe(32)}"
    with _SsoCallbackServer(callback_path) as server:
        host, port = cast("tuple[str, int]", server.server_address)
        redirect_url = f"http://{host}:{port}{callback_path}"
        login_url = matrix_sso_login_url(homeserver, redirect_url=redirect_url, idp_id=idp_id)

        opened = False
        if open_browser:
            opener = browser_opener or webbrowser.open
            try:
                opened = opener(login_url)
            except (OSError, webbrowser.Error):
                opened = False
        if opened:
            announce(
                "Browser opened for Matrix SSO. Complete sign-in there; this command will continue automatically. "
                "Press Ctrl-C to cancel.",
            )
        else:
            announce(f"Waiting for Matrix SSO. Press Ctrl-C to cancel.\nOpen this URL in a browser:\n{login_url}")

        while server.login_token is None and server.callback_error is None:
            server.handle_request()

        if server.callback_error is not None:
            raise DesktopSsoError(server.callback_error)
        if server.login_token is None:  # pragma: no cover - loop invariant
            msg = "Matrix SSO callback completed without a login token."
            raise DesktopSsoError(msg)
        return server.login_token


__all__ = ["DesktopSsoError", "matrix_sso_login_url", "receive_sso_login_token"]

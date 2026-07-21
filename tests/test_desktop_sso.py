"""Tests for browser-assisted Matrix SSO login."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from mindroom.desktop.sso import DesktopSsoError, matrix_sso_login_url, receive_sso_login_token

if TYPE_CHECKING:
    from collections.abc import Callable


def _callback_from_login_url(login_url: str) -> str:
    redirect_urls = parse_qs(urlsplit(login_url).query).get("redirectUrl", [])
    assert len(redirect_urls) == 1
    return redirect_urls[0]


def _request_in_thread(url_factory: Callable[[], str]) -> threading.Thread:
    def request() -> None:
        try:
            with urlopen(url_factory(), timeout=2) as response:  # noqa: S310 - Test-only loopback callback.
                response.read()
        except HTTPError as exc:
            exc.read()

    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    return thread


def test_matrix_sso_login_url_encodes_exact_loopback_redirect() -> None:
    """Homeserver receives the complete callback URL as one redirectUrl value."""
    callback = "http://127.0.0.1:54321/_mindroom/matrix-sso/random/path"

    login_url = matrix_sso_login_url("https://matrix.example.org/", redirect_url=callback)

    parsed = urlsplit(login_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "matrix.example.org"
    assert parsed.path == "/_matrix/client/v3/login/sso/redirect"
    assert parse_qs(parsed.query) == {"redirectUrl": [callback]}


def test_matrix_sso_login_url_encodes_identity_provider_in_path() -> None:
    """An explicit provider uses the Matrix IdP-specific SSO endpoint."""
    login_url = matrix_sso_login_url(
        "https://matrix.example.org",
        redirect_url="http://127.0.0.1:54321/callback",
        idp_id="work account/primary",
    )

    assert urlsplit(login_url).path == "/_matrix/client/v3/login/sso/redirect/work%20account%2Fprimary"


def test_sso_callback_returns_token_without_logging_it() -> None:
    """One exact callback transfers the short-lived token only through memory."""
    notices: list[str] = []
    request_thread: threading.Thread | None = None

    def open_browser(login_url: str) -> bool:
        nonlocal request_thread
        callback = _callback_from_login_url(login_url)
        request_thread = _request_in_thread(
            lambda: f"{callback}?{urlencode({'loginToken': 'short-lived-secret'})}",
        )
        return True

    token = receive_sso_login_token(
        "https://matrix.example.org",
        open_browser=True,
        announce=notices.append,
        browser_opener=open_browser,
    )

    assert token == "short-lived-secret"  # noqa: S105 - Test-only login token.
    assert notices == [
        "Browser opened for Matrix SSO. Complete sign-in there; this command will continue automatically.",
    ]
    assert all(token not in notice for notice in notices)
    assert request_thread is not None
    request_thread.join(timeout=2)
    assert not request_thread.is_alive()


def test_manual_sso_prints_login_url_and_accepts_callback() -> None:
    """Headless users can copy the URL without invoking a browser helper."""
    notices: list[str] = []
    request_thread: threading.Thread | None = None

    def announce(message: str) -> None:
        nonlocal request_thread
        notices.append(message)
        login_url = message.splitlines()[-1]
        callback = _callback_from_login_url(login_url)
        request_thread = _request_in_thread(lambda: f"{callback}?loginToken=manual-token")

    token = receive_sso_login_token(
        "https://matrix.example.org",
        open_browser=False,
        announce=announce,
        browser_opener=lambda _url: pytest.fail("browser opener must not run"),
    )

    assert token == "manual-token"  # noqa: S105 - Test-only login token.
    assert notices[0].startswith("Open this URL in a browser")
    assert request_thread is not None
    request_thread.join(timeout=2)
    assert not request_thread.is_alive()


def test_sso_callback_rejects_missing_token() -> None:
    """A callback reaching the capability path without a token fails closed."""
    request_thread: threading.Thread | None = None

    def open_browser(login_url: str) -> bool:
        nonlocal request_thread
        callback = _callback_from_login_url(login_url)
        request_thread = _request_in_thread(lambda: callback)
        return True

    with pytest.raises(DesktopSsoError, match="exactly one login token"):
        receive_sso_login_token(
            "https://matrix.example.org",
            open_browser=True,
            announce=lambda _message: None,
            browser_opener=open_browser,
        )

    assert request_thread is not None
    request_thread.join(timeout=2)
    assert not request_thread.is_alive()

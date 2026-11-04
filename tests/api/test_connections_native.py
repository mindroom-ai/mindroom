"""Native sign-in: MindRoom Chat's iOS app confirms the browser session it opens with its own Matrix OpenID token."""

# The imported sign-in fixture is intentionally shadowed by pytest injection.
# ruff: noqa: F811

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.api import connections_native, main
from mindroom.api.connections_native import _validated_native_next
from mindroom.api.connections_session_cookie import read_connections_session
from tests.api.test_connections_signin import (
    CONNECTIONS_SESSION_COOKIE,
    PORTAL_ORIGIN,
    _serve,
    _with_admin,
    signin,  # noqa: F401
)

if TYPE_CHECKING:
    from httpx import Response

    from mindroom.api.connections_session_cookie import _ConnectionsSession

NATIVE = "/api/connections/session/native"
PENDING_COOKIE = f"{CONNECTIONS_SESSION_COOKIE}_native"
CONFIRM_PREFIX = "mindroom://connections/confirm?nonce="
CONNECT_LINK = "/api/oauth/google_drive/authorize?connect_token=x&execution_scope=user_agent"
TAP_AGAIN = "Tap the link in the MindRoom app again."


def _token(name: str) -> dict[str, Any]:
    return {"access_token": name, "token_type": "Bearer", "matrix_server_name": "example.org", "expires_in": 300}


def _app(browser: TestClient) -> TestClient:
    """The MindRoom app beside `browser`: it calls the same backend but shares no cookies with the browser."""
    return TestClient(main.app, base_url=str(browser.base_url))


def native_start(browser: TestClient, name: str = "alice", next_path: str | None = None) -> Response:
    """Ask for a redeem URL with `name`'s token, the way the app does before it opens the first sign-in sheet."""
    body: dict[str, Any] = {"openid_token": _token(name)}
    if next_path is not None:
        body["next"] = next_path
    return _app(browser).post(f"{NATIVE}/start", json=body)


def redeem(browser: TestClient, started: Response) -> Response:
    """Open the redeem URL in `browser`, the first sign-in sheet."""
    return browser.get(started.json()["redeem_url"], follow_redirects=False)


def nonce_of(redeemed: Response) -> str:
    """Return the nonce the redeem step hands to the app through its custom-scheme callback."""
    location = redeemed.headers["location"]
    assert location.startswith(CONFIRM_PREFIX), location
    return location.removeprefix(CONFIRM_PREFIX)


def confirm(browser: TestClient, nonce: str, name: str = "alice") -> Response:
    """Confirm `nonce` from the app with `name`'s token."""
    return _app(browser).post(f"{NATIVE}/confirm", json={"openid_token": _token(name), "nonce": nonce})


def continue_in(browser: TestClient, confirmed: Response) -> Response:
    """Open the continue URL in `browser`, the second sign-in sheet."""
    return browser.get(confirmed.json()["continue_url"], follow_redirects=False)


def run_native_flow(client: TestClient, name: str = "alice", next_path: str | None = None) -> Response:
    """Run start, redeem, confirm, and continue as `name` with `client` as the browser; return the continue response."""
    started = native_start(client, name, next_path)
    assert started.status_code == 200, started.text
    redeemed = redeem(client, started)
    assert redeemed.status_code == 303, redeemed.text
    confirmed = confirm(client, nonce_of(redeemed), name)
    assert confirmed.status_code == 200, confirmed.text
    return continue_in(client, confirmed)


def _session_of(browser: TestClient, cookie: str = CONNECTIONS_SESSION_COOKIE) -> _ConnectionsSession | None:
    token = browser.cookies.get(cookie)
    return read_connections_session(token, now=time.time()) if token else None


def _expect_tap_again_page(response: Response) -> None:
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert TAP_AGAIN in response.text
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers


def test_native_flow_signs_browser_in_and_opens_next(signin: dict[str, Any]) -> None:
    """The browser that redeemed the start gets a portal-only session once the app confirms, then opens `next`.

    Alice is an administrator here, so the session's missing dashboard bit is this flow's choice.
    """
    _with_admin(signin)
    signin["binds"].return_value = True
    browser = signin["client"]
    started = native_start(browser, next_path=CONNECT_LINK)
    assert started.status_code == 200, started.text
    assert started.json()["redeem_url"].startswith(f"{PORTAL_ORIGIN}{NATIVE}/redeem?start=")
    assert "no-store" in started.headers["cache-control"]

    redeemed = redeem(browser, started)
    assert redeemed.status_code == 303, redeemed.text
    assert redeemed.headers["location"].startswith(CONFIRM_PREFIX)
    assert "no-store" in redeemed.headers["cache-control"]
    assert redeemed.headers["referrer-policy"] == "no-referrer"
    pending = redeemed.headers["set-cookie"]
    for attribute in (f"{PENDING_COOKIE}=", "Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=120"):
        assert attribute in pending
    assert _session_of(browser) is None

    confirmed = confirm(browser, nonce_of(redeemed))
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["continue_url"].startswith(f"{PORTAL_ORIGIN}{NATIVE}/continue?confirmed=")
    assert "no-store" in confirmed.headers["cache-control"]

    continued = continue_in(browser, confirmed)
    assert continued.status_code == 303, continued.text
    assert continued.headers["location"] == CONNECT_LINK
    assert continued.headers["referrer-policy"] == "no-referrer"
    session = _session_of(browser)
    assert session is not None
    assert session.matrix_user_id == "@alice:example.org"
    assert session.dashboard is False
    assert browser.cookies.get(PENDING_COOKIE) is None
    assert any(
        cookie.startswith(f"{PENDING_COOKIE}=") and "Max-Age=0" in cookie
        for cookie in continued.headers.get_list("set-cookie")
    )


def test_native_flow_opens_the_portal_by_default(signin: dict[str, Any]) -> None:
    """Without `next`, the Settings card's sign-in lands on the portal."""
    signin["binds"].return_value = True
    continued = run_native_flow(signin["client"])
    assert continued.status_code == 303, continued.text
    assert continued.headers["location"] == "/connections/"


def test_native_start_requires_binding(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Without binding, even with a Chat allowlist configured, neither app call verifies a token or issues a state."""
    issue = Mock(wraps=connections_native.issue_opaque_oauth_state)
    monkeypatch.setattr(connections_native, "issue_opaque_oauth_state", issue)
    started = native_start(signin["client"])
    assert started.status_code == 403, started.text
    assert started.json() == {"detail": "Sign-in from the MindRoom app needs a homeserver that binds tokens"}
    assert confirm(signin["client"], "any-nonce").status_code == 403
    issue.assert_not_called()
    signin["verifier"].assert_not_awaited()


def test_native_start_rejects_unknown_token(signin: dict[str, Any]) -> None:
    """A token the homeserver does not vouch for gets the same 401 as the web sign-in."""
    signin["binds"].return_value = True
    response = native_start(signin["client"], "nobody")
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Matrix OpenID verification failed."}


def test_native_start_rejects_user_without_connections(signin: dict[str, Any]) -> None:
    """Any homeserver user has a valid token, so one without portal access gets no redeem URL."""
    signin["binds"].return_value = True
    response = native_start(signin["client"], "mallory")
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "No connections are available for this account"}


def test_native_start_refuses_off_portal_next(signin: dict[str, Any]) -> None:
    """A `next` outside the portal and OAuth link paths is refused before the token is verified."""
    signin["binds"].return_value = True
    response = native_start(signin["client"], next_path="//evil.example")
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "Invalid next path"}
    signin["verifier"].assert_not_awaited()


def test_forwarded_redeem_cannot_be_confirmed_by_another_user(signin: dict[str, Any]) -> None:
    """Alice forwards her redeem URL: the nonce reaches the victim's device, whose app cannot confirm it as Bob.

    Nor can Alice then sign the victim's browser in with a continue URL from a second attempt in her own browser.
    """
    signin["binds"].return_value = True
    attacker = signin["client"]
    victim = TestClient(main.app, base_url=PORTAL_ORIGIN)
    redeemed = redeem(victim, native_start(attacker))
    assert redeemed.status_code == 303, redeemed.text
    nonce = nonce_of(redeemed)

    refused = confirm(victim, nonce, "bob")
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "This sign-in was started by another account"}
    retried = confirm(attacker, nonce)
    assert retried.status_code == 400, retried.text

    own_attempt = confirm(attacker, nonce_of(redeem(attacker, native_start(attacker))))
    assert own_attempt.status_code == 200, own_attempt.text
    _expect_tap_again_page(continue_in(victim, own_attempt))
    assert _session_of(victim) is None


def test_continue_requires_the_redeeming_browser(signin: dict[str, Any]) -> None:
    """A continue URL works only once, and only in the browser holding the pending cookie it was confirmed for."""
    signin["binds"].return_value = True
    browser = signin["client"]
    other = TestClient(main.app, base_url=PORTAL_ORIGIN)
    confirmed = confirm(browser, nonce_of(redeem(browser, native_start(browser))))
    assert confirmed.status_code == 200, confirmed.text
    _expect_tap_again_page(continue_in(other, confirmed))
    assert _session_of(other) is None
    # The refused attempt spent the URL, so even the browser that redeemed it cannot use it now.
    _expect_tap_again_page(continue_in(browser, confirmed))
    assert _session_of(browser) is None

    succeeded = run_native_flow(browser)
    assert succeeded.status_code == 303, succeeded.text
    replayed = browser.get(str(succeeded.request.url), follow_redirects=False)
    _expect_tap_again_page(replayed)


def test_new_attempt_replaces_abandoned_pending_cookie(signin: dict[str, Any]) -> None:
    """A new redeem overwrites an abandoned attempt's pending cookie; the old continue URL fails, the new one works."""
    signin["binds"].return_value = True
    browser = signin["client"]
    abandoned = confirm(browser, nonce_of(redeem(browser, native_start(browser))))
    assert abandoned.status_code == 200, abandoned.text
    confirmed = confirm(browser, nonce_of(redeem(browser, native_start(browser, next_path=CONNECT_LINK))))
    assert confirmed.status_code == 200, confirmed.text

    _expect_tap_again_page(continue_in(browser, abandoned))
    assert _session_of(browser) is None
    continued = continue_in(browser, confirmed)
    assert continued.status_code == 303, continued.text
    assert continued.headers["location"] == CONNECT_LINK
    session = _session_of(browser)
    assert session is not None
    assert session.matrix_user_id == "@alice:example.org"


def test_expired_states_are_refused(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonce confirmed more than 120 seconds after the redeem is refused."""
    signin["binds"].return_value = True
    browser = signin["client"]
    redeemed = redeem(browser, native_start(browser))
    assert redeemed.status_code == 303, redeemed.text
    later = time.time() + 121
    monkeypatch.setattr(time, "time", lambda: later)
    refused = confirm(browser, nonce_of(redeemed))
    assert refused.status_code == 400, refused.text
    assert refused.json() == {"detail": "This sign-in expired or was already used"}


def test_native_flow_on_plain_http_local_backend(signin: dict[str, Any]) -> None:
    """A local backend over plain HTTP drops `Secure` and the `__Host-` prefix from both cookies, so browsers keep them."""
    local = "http://localhost:8765"
    _serve(signin, MINDROOM_PUBLIC_URL=local)
    signin["binds"].return_value = True
    browser = TestClient(main.app, base_url=local)
    started = native_start(browser)
    assert started.status_code == 200, started.text
    assert started.json()["redeem_url"].startswith(f"{local}{NATIVE}/redeem?start=")
    redeemed = redeem(browser, started)
    assert redeemed.status_code == 303, redeemed.text
    pending = redeemed.headers["set-cookie"]
    assert pending.startswith("mindroom_connections_session_native=")
    assert "Secure" not in pending

    confirmed = confirm(browser, nonce_of(redeemed))
    assert confirmed.json()["continue_url"].startswith(f"{local}{NATIVE}/continue?confirmed=")
    continued = continue_in(browser, confirmed)
    assert continued.status_code == 303, continued.text
    cookies = continued.headers.get_list("set-cookie")
    assert not any("__Host-" in cookie or "Secure" in cookie for cookie in cookies)
    session = _session_of(browser, "mindroom_connections_session")
    assert session is not None
    assert session.matrix_user_id == "@alice:example.org"
    assert browser.cookies.get("mindroom_connections_session_native") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/connections/"),
        ("/connections/", "/connections/"),
        ("/api/oauth/github/reset?reset_token=y", "/api/oauth/github/reset?reset_token=y"),
        (CONNECT_LINK, CONNECT_LINK),
        ("/%2F%2Fevil.example", None),
        ("/%2f%2fevil.example", None),
        ("//evil.example", None),
        ("/api/oauth/x/authorize/../../login", None),
        ("\\evil", None),
        ("\\\\evil", None),
        ("/\\evil.example", None),
        ("https://evil.example/connections/", None),
        ("/api/oauth/x/callback?code=1", None),
        ("/api/oauth/x/authorize?next=%5cevil", None),
        ("/api/oauth/x/authorize?next=https://evil.example", None),
        ("/api/oauth/x/authorize#fragment", None),
        ("/api/oauth/x/authorize?a=1\r\nSet-Cookie: b=2", None),
        ("/api/oauth/x.y/authorize", None),
        ("/connections", None),
        ("", None),
    ],
)
def test_validated_native_next(value: str | None, expected: str | None) -> None:
    """Only the portal and OAuth connect or reset links, as same-origin paths, are accepted as `next`."""
    if expected is not None:
        assert _validated_native_next(value) == expected
        return
    with pytest.raises(HTTPException) as refused:
        _validated_native_next(value)
    assert refused.value.status_code == 400
    assert refused.value.detail == "Invalid next path"


def test_native_errors_do_not_echo_tokens(signin: dict[str, Any]) -> None:
    """No refusal repeats a submitted token or nonce."""
    signin["binds"].return_value = True
    secret = "not-a-user-secret"  # noqa: S105
    app = _app(signin["client"])
    rejected = app.post(f"{NATIVE}/start", json={"openid_token": _token(secret)})
    assert rejected.status_code == 401, rejected.text
    malformed = app.post(f"{NATIVE}/start", json={"openid_token": _token(secret), "unexpected": True})
    assert malformed.status_code == 401, malformed.text
    assert malformed.json() == {"detail": "Invalid sign-in request"}
    not_json = app.post(
        f"{NATIVE}/start",
        content=f'{{"openid_token": "{secret}"',
        headers={"Content-Type": "application/json"},
    )
    assert not_json.status_code == 401, not_json.text
    unknown_nonce = confirm(signin["client"], secret)
    assert unknown_nonce.status_code == 400, unknown_nonce.text
    nonce = nonce_of(redeem(signin["client"], native_start(signin["client"])))
    bad_token = confirm(signin["client"], nonce, secret)
    assert bad_token.status_code == 401, bad_token.text
    for response in (rejected, malformed, not_json, unknown_nonce, bad_token):
        assert secret not in response.text
        assert nonce not in response.text
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("path", ["redeem?start=unknown-secret", "continue?confirmed=unknown-secret"])
def test_browser_refusals_ask_to_tap_the_link_again(signin: dict[str, Any], path: str) -> None:
    """An unknown, spent, or expired state in a browser step renders a page, not JSON, and never repeats the state."""
    response = signin["client"].get(f"{NATIVE}/{path}", follow_redirects=False)
    _expect_tap_again_page(response)
    assert "unknown-secret" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "start"), ("GET", "redeem?start=s"), ("POST", "confirm"), ("GET", "continue?confirmed=k")],
)
def test_native_sign_in_refuses_plain_http_outside_local_addresses(
    signin: dict[str, Any],
    method: str,
    path: str,
) -> None:
    """A cleartext public origin gets no native sign-in step at all."""
    public_url = "http://portal.example.org"
    _serve(signin, MINDROOM_PUBLIC_URL=public_url)
    signin["binds"].return_value = True
    body = {"openid_token": _token("alice"), "nonce": "n"} if path == "confirm" else {"openid_token": _token("alice")}
    client = TestClient(main.app, base_url=public_url)
    response = client.request(method, f"{NATIVE}/{path}", json=body if method == "POST" else None)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "Connections require an HTTPS public origin, or HTTP on a local address"}
    signin["verifier"].assert_not_awaited()

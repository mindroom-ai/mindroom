"""Client sessions trade a Matrix OpenID token for a bearer token that plugin routes accept."""

import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import pytest
from aiohttp import web
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mindroom.api import client_sessions, config_lifecycle
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import ClientSessionStore
from mindroom.api.main import _RuntimeDashboardCorsMiddleware
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.plugin_http import ClientUser, require_client_user

OPENID_TOKEN = {
    "access_token": "openid-secret",
    "token_type": "Bearer",
    "matrix_server_name": "example.org",
    "expires_in": 60,
}
CLIENT_ORIGIN = "https://chat.example.org"


class HomeserverPeer:
    """Fake homeserver answering `/openid/userinfo` and `/versions`, which says whether it binds tokens.

    The token it knows is bound to this deployment unless ``token_bound`` is off, as for one replayed from another
    backend: while binding, the homeserver refuses such a token.
    """

    def __init__(self) -> None:
        self.status = 200
        self.subject = "@alice:example.org"
        self.binding = True
        self.token_bound = True
        self.versions_status = 200
        # Runs right after `/versions` is answered, to change the homeserver between the exchange's requests.
        self.after_versions: Callable[[HomeserverPeer], None] | None = None

    async def userinfo(self, request: web.Request) -> web.Response:
        """Answer like Synapse: the subject for the right token, 401 otherwise."""
        if self.status != 200 or request.query.get("access_token") != "openid-secret":
            return web.Response(status=401)
        if self.binding and not self.token_bound:
            return web.json_response({"errcode": "M_UNKNOWN_TOKEN"}, status=401)
        return web.json_response({"sub": self.subject})

    async def versions(self, _request: web.Request) -> web.Response:
        """List binding in `unstable_features` while it is on, like MindRoom's homeserver fork."""
        if self.versions_status != 200:
            return web.Response(status=self.versions_status)
        response = web.json_response(
            {"versions": ["v1.11"], "unstable_features": {"io.mindroom.openid_audience": self.binding}},
        )
        if self.after_versions is not None:
            self.after_versions(self)
        return response


@pytest.fixture
def peer() -> HomeserverPeer:
    """Fake homeserver peer."""
    return HomeserverPeer()


@pytest.fixture
def client(tmp_path: Path, peer: HomeserverPeer) -> Iterator[TestClient]:
    """App with the session router, a throwaway protected route and the real CORS middleware."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upstream = web.Application()
        upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", peer.userinfo)
        upstream.router.add_get("/_matrix/client/versions", peer.versions)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
        paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={
                "MATRIX_HOMESERVER": origin,
                "MATRIX_SERVER_NAME": "example.org",
                "MINDROOM_COMPUTER_ALLOWED_ORIGINS": f'["{CLIENT_ORIGIN}"]',
            },
        )
        ensure_app_state(app).api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, paths, runtime_config=Config()),
        )
        try:
            yield
        finally:
            await runner.cleanup()

    app = FastAPI(lifespan=lifespan)
    app.include_router(client_sessions.router)

    @app.get("/api/plugins/x/whoami")
    async def whoami(user: Annotated[ClientUser, Depends(require_client_user)]) -> dict[str, str]:
        return {"user_id": user.user_id}

    @app.get("/api/plugins/x/page")
    async def page() -> HTMLResponse:
        return HTMLResponse("<script>steal()</script>", headers={"Content-Security-Policy": "default-src *"})

    app.add_middleware(
        _RuntimeDashboardCorsMiddleware,
        api_app=app,
        fallback_runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
    )
    with TestClient(app) as test_client:
        yield test_client


def exchange(client: TestClient) -> dict[str, object]:
    """Trade the valid OpenID token for a session."""
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 200, response.text
    return response.json()


def test_exchange_returns_session_for_openid_user(client: TestClient) -> None:
    """A valid OpenID token yields the user's id, a session token and a one-hour expiry."""
    body = exchange(client)
    assert body["user_id"] == "@alice:example.org"
    assert isinstance(body["session_token"], str)
    assert body["session_token"]
    assert abs(body["expires_at"] - (time.time() + 3600)) <= 5


def test_exchange_rejects_bad_openid(client: TestClient, peer: HomeserverPeer) -> None:
    """A homeserver that rejects the token gives no session, and the answer does not echo the OpenID token."""
    peer.status = 401
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 401
    assert "session_token" not in response.text
    assert OPENID_TOKEN["access_token"] not in response.text


def test_require_client_user_accepts_bearer(client: TestClient) -> None:
    """A plugin route behind `require_client_user` sees the session's user."""
    token = exchange(client)["session_token"]
    response = client.get("/api/plugins/x/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"user_id": "@alice:example.org"}


def test_require_client_user_401_without_or_with_unknown_token(client: TestClient) -> None:
    """No header, a non-bearer header or an unknown token is a 401."""
    assert client.get("/api/plugins/x/whoami").status_code == 401
    assert client.get("/api/plugins/x/whoami", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/api/plugins/x/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_expired_session_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session that works until its one-hour life is over is a 401 after it."""
    token = exchange(client)["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    store = client.app.state.mindroom_app_state.client_sessions
    almost = time.time() + 3590
    monkeypatch.setattr(store, "clock", lambda: almost)
    assert client.get("/api/plugins/x/whoami", headers=headers).status_code == 200
    later = time.time() + 3601
    monkeypatch.setattr(store, "clock", lambda: later)
    response = client.get("/api/plugins/x/whoami", headers=headers)
    assert response.status_code == 401


def test_revoked_session_is_rejected_at_once(client: TestClient) -> None:
    """A client that revokes its session, as at sign-out, gets 401 for that token from then on, revoking included."""
    headers = {"Authorization": f"Bearer {exchange(client)['session_token']}"}
    assert client.get("/api/plugins/x/whoami", headers=headers).status_code == 200
    revoked = client.delete("/api/client-sessions", headers=headers)
    assert revoked.status_code == 204
    assert not revoked.content
    assert client.get("/api/plugins/x/whoami", headers=headers).status_code == 401
    assert client.delete("/api/client-sessions", headers=headers).status_code == 401


def test_revoke_ends_only_the_presented_session(client: TestClient) -> None:
    """Revoking one of a user's sessions, such as one device's, leaves that user's other sessions working."""
    first, second = ({"Authorization": f"Bearer {exchange(client)['session_token']}"} for _ in range(2))
    assert client.delete("/api/client-sessions", headers=first).status_code == 204
    assert client.get("/api/plugins/x/whoami", headers=first).status_code == 401
    assert client.get("/api/plugins/x/whoami", headers=second).json() == {"user_id": "@alice:example.org"}


def test_revoke_needs_a_live_session(client: TestClient) -> None:
    """Revoking with no session, a non-bearer header or an unknown token is a 401 that ends no session."""
    headers = {"Authorization": f"Bearer {exchange(client)['session_token']}"}
    for bad in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nope"}):
        response = client.delete("/api/client-sessions", headers=bad)
        assert response.status_code == 401, bad
        assert response.headers["www-authenticate"] == "Bearer", bad
    assert client.get("/api/plugins/x/whoami", headers=headers).status_code == 200


def test_cors_allows_client_origin_on_plugin_paths(client: TestClient) -> None:
    """Only origins in MINDROOM_COMPUTER_ALLOWED_ORIGINS pass a preflight on plugin, session and room paths."""
    for path, method in (
        ("/api/plugins/x/y", "POST"),
        ("/api/client-sessions", "POST"),
        ("/api/client-sessions", "DELETE"),
        ("/api/rooms/x/agents/y/terminals", "POST"),
        ("/api/rooms/x/agents/y/terminals/z", "DELETE"),
    ):
        headers = {"Access-Control-Request-Method": method, "Access-Control-Request-Headers": "authorization"}
        allowed = client.options(path, headers={**headers, "Origin": CLIENT_ORIGIN})
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == CLIENT_ORIGIN
        denied = client.options(path, headers={**headers, "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in denied.headers


def test_client_paths_are_never_cached_nor_credentialed(client: TestClient) -> None:
    """Session and plugin answers carry no-store and never allow credentials, preflight or not."""
    token = exchange(client)["session_token"]
    origin = {"Origin": CLIENT_ORIGIN}
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    responses = [
        client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN}, headers=origin),
        client.get("/api/plugins/x/whoami", headers={**origin, "Authorization": f"Bearer {token}"}),
        client.get("/api/plugins/x/whoami", headers=origin),
        client.options("/api/client-sessions", headers={**origin, **preflight}),
        client.options("/api/plugins/x/whoami", headers={**origin, **preflight}),
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.request.url
        assert response.headers["access-control-allow-origin"] == CLIENT_ORIGIN, response.request.url
        assert "access-control-allow-credentials" not in response.headers, response.request.url


def test_client_paths_keep_their_policy_under_a_proxy_root_path(client: TestClient) -> None:
    """Served under a proxy's root path, session and plugin paths keep the client policy, preflight or not."""
    proxied = TestClient(client.app, root_path="/mindroom")
    origin = {"Origin": CLIENT_ORIGIN}
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    signed_in = proxied.post("/mindroom/api/client-sessions", json={"openid_token": OPENID_TOKEN}, headers=origin)
    bearer = {"Authorization": f"Bearer {signed_in.json()['session_token']}"}
    responses = [
        signed_in,
        proxied.get("/mindroom/api/plugins/x/whoami", headers={**origin, **bearer}),
        proxied.options("/mindroom/api/client-sessions", headers={**origin, **preflight}),
        proxied.options("/mindroom/api/plugins/x/whoami", headers={**origin, **preflight}),
    ]
    for response in responses:
        assert response.status_code == 200, response.request.url
        assert response.headers["cache-control"] == "no-store", response.request.url
        assert response.headers["access-control-allow-origin"] == CLIENT_ORIGIN, response.request.url
        assert "access-control-allow-credentials" not in response.headers, response.request.url
    assert responses[1].json() == {"user_id": "@alice:example.org"}
    assert responses[1].headers["x-content-type-options"] == "nosniff"
    denied = proxied.options("/mindroom/api/plugins/x/whoami", headers={**preflight, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


def test_plugin_answers_never_sniff_nor_render(client: TestClient) -> None:
    """Plugin answers, even HTML a route sends with its own policy, are nosniff and sandboxed with nothing allowed."""
    for response in (client.get("/api/plugins/x/page"), client.get("/api/plugins/x/whoami")):
        assert response.headers["x-content-type-options"] == "nosniff", response.request.url
        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'", response.request.url
        assert response.headers.get_list("content-security-policy") == ["sandbox; default-src 'none'"]
    session = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert "content-security-policy" not in session.headers


def test_client_origin_paths_stop_at_their_own_segment(client: TestClient) -> None:
    """A sibling path that only starts with a client path's letters gets the dashboard's policy, not the client's."""
    headers = {
        "Origin": CLIENT_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    for path in ("/api/pluginsX/y", "/api/client-sessionsX", "/api/computersX", "/api/roomsX"):
        preflight = client.options(path, headers=headers)
        assert "access-control-allow-origin" not in preflight.headers, path
        assert preflight.headers.get("cache-control") != "no-store", path


def test_exchange_refuses_homeserver_that_does_not_bind_tokens(client: TestClient, peer: HomeserverPeer) -> None:
    """Like Connections, an unbound token is refused with 403 even when the homeserver would accept it."""
    peer.binding = False
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 403
    assert "session_token" not in response.text
    assert OPENID_TOKEN["access_token"] not in response.text


def test_exchange_answers_503_when_binding_cannot_be_checked(client: TestClient, peer: HomeserverPeer) -> None:
    """A `/versions` that fails leaves binding unknown: the client may retry, so it is 503 rather than 403."""
    peer.versions_status = 502
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 503
    assert "session_token" not in response.text


def test_binding_switched_off_during_exchange_gives_no_session(client: TestClient, peer: HomeserverPeer) -> None:
    """A token replayed from another backend gets no session when binding turns off right after `/versions` answers.

    A probe before a single verification would see binding on and then verify the token after binding went off.
    """
    peer.token_bound = False

    def switch_off(homeserver: HomeserverPeer) -> None:
        homeserver.binding = False

    peer.after_versions = switch_off
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 401
    assert "session_token" not in response.text


def test_exchange_needs_the_same_user_from_both_verifications(client: TestClient, peer: HomeserverPeer) -> None:
    """A token whose second verification names another user gets no session."""

    def other_user(homeserver: HomeserverPeer) -> None:
        homeserver.subject = "@mallory:example.org"

    peer.after_versions = other_user
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 401
    assert "session_token" not in response.text


class Clock:
    """A settable clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


def test_session_repr_leaves_out_its_token() -> None:
    """A logged or printed session never shows its bearer token."""
    session = ClientSessionStore().create("@alice:example.org")
    assert session.token not in repr(session)
    assert "@alice:example.org" in repr(session)


def test_per_user_cap_evicts_that_users_oldest_session() -> None:
    """A session past the per-user cap drops that user's first and leaves other users alone."""
    store = ClientSessionStore(per_user_capacity=2)
    bob = store.create("@bob:example.org")
    first = store.create("@alice:example.org")
    second = store.create("@alice:example.org")
    third = store.create("@alice:example.org")
    assert store.lookup(first.token) is None
    assert store.lookup(second.token) == second
    assert store.lookup(third.token) == third
    assert store.lookup(bob.token) == bob


def test_exchanges_below_the_global_cap_never_evict_another_users_session() -> None:
    """Many users signing in up to their own cap, together below the global cap, leave every other session alone."""
    store = ClientSessionStore()
    bob = store.create("@bob:example.org")
    for i in range(40):
        for _ in range(store.per_user_capacity + 2):
            store.create(f"@user{i}:example.org")
    assert store.lookup(bob.token) == bob


def test_global_cap_evicts_the_oldest_session() -> None:
    """At the global cap a new session from anyone evicts the globally oldest one, never refusing."""
    store = ClientSessionStore(capacity=3)
    sessions = [store.create(f"@user{i}:example.org") for i in range(4)]
    assert store.lookup(sessions[0].token) is None
    assert [store.lookup(session.token) for session in sessions[1:]] == sessions[1:]


def test_expired_token_is_none_and_expired_sessions_free_their_slots() -> None:
    """An expired token gives None, and expired sessions do not count against the caps."""
    clock = Clock()
    store = ClientSessionStore(clock=clock, capacity=2, per_user_capacity=2)
    old = [store.create("@alice:example.org"), store.create("@bob:example.org")]
    clock.now += 3600
    assert store.lookup(old[0].token) is None
    fresh = store.create("@carol:example.org")
    assert store.lookup(old[1].token) is None
    assert store.lookup(fresh.token) == fresh
    assert store.lookup("never-issued") is None

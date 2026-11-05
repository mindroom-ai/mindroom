"""Client sessions trade a Matrix OpenID token for a bearer token that plugin routes accept."""

import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from fastapi import Depends, FastAPI
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


class UserinfoPeer:
    """Fake homeserver answering `/openid/userinfo`."""

    def __init__(self) -> None:
        self.status = 200

    async def userinfo(self, request: web.Request) -> web.Response:
        """Answer like Synapse: the subject for the right token, 401 otherwise."""
        if self.status != 200 or request.query.get("access_token") != "openid-secret":
            return web.Response(status=401)
        return web.json_response({"sub": "@alice:example.org"})


@pytest.fixture
def peer() -> UserinfoPeer:
    """Fake homeserver peer."""
    return UserinfoPeer()


@pytest.fixture
def client(tmp_path: Path, peer: UserinfoPeer, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """App with the session router, a throwaway protected route and the real CORS middleware."""
    monkeypatch.setattr(client_sessions, "homeserver_binds_openid_audience", AsyncMock(return_value=True))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upstream = web.Application()
        upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", peer.userinfo)
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


def test_exchange_rejects_bad_openid(client: TestClient, peer: UserinfoPeer) -> None:
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


def test_cors_allows_client_origin_on_plugin_paths(client: TestClient) -> None:
    """Only origins in MINDROOM_COMPUTER_ALLOWED_ORIGINS pass a preflight on plugin and session paths."""
    for path in ("/api/plugins/x/y", "/api/client-sessions"):
        headers = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
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


def test_client_origin_paths_stop_at_their_own_segment(client: TestClient) -> None:
    """A sibling path that only starts with a client path's letters gets the dashboard's policy, not the client's."""
    headers = {
        "Origin": CLIENT_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    for path in ("/api/pluginsX/y", "/api/client-sessionsX", "/api/computersX"):
        preflight = client.options(path, headers=headers)
        assert "access-control-allow-origin" not in preflight.headers, path
        assert preflight.headers.get("cache-control") != "no-store", path


def test_exchange_refuses_homeserver_that_does_not_bind_tokens(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like Connections, an unbound token is refused with 403 even when the homeserver would accept it."""
    monkeypatch.setattr(client_sessions, "homeserver_binds_openid_audience", AsyncMock(return_value=False))
    response = client.post("/api/client-sessions", json={"openid_token": OPENID_TOKEN})
    assert response.status_code == 403
    assert "session_token" not in response.text
    assert OPENID_TOKEN["access_token"] not in response.text


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

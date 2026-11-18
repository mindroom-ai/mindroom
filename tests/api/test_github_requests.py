"""A signed-in client's GitHub requests go out with its user's own connected GitHub account, which stays here."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from mindroom.api import config_lifecycle, github_requests
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.api.main import _RuntimeDashboardCorsMiddleware
from mindroom.api.validation_errors import validation_error_response
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import load_oauth_credentials_snapshot_sync, resolve_oauth_credential_context
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthProviderError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import corrupt_oauth_credential_payload, publish_oauth_credentials, serve_token_endpoint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
CLIENT_ORIGIN = "https://chat.example.org"
ALICE_TOKEN = "gho-alice-access"  # noqa: S105
SHARED_TOKEN = "ghp-shared-token"  # noqa: S105
ALICE_PAT = "github_pat_alice-personal"
DEFAULT_ACCEPT = "application/vnd.github+json"

type Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class GitHubPeer:
    """Fake api.github.com that records each request and answers with ``handler``, by default a JSON 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda _request: httpx.Response(200, json={"ok": True})

    def handle(self, request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        """Record ``request`` and answer it."""
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """A runtime with a GitHub App client, whose environment also carries a shared GitHub token."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.org",
            "MINDROOM_COMPUTER_ALLOWED_ORIGINS": f'["{CLIENT_ORIGIN}"]',
            "GITHUB_ACCESS_TOKEN": SHARED_TOKEN,
            "GITHUB_TOKEN": SHARED_TOKEN,
        },
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("github_oauth_client", {"client_id": "github-client-id", "client_secret": "secret"})
    # The `github` tool's one-token-for-everyone setting must never answer for a person.
    manager.save_credentials("github", {"access_token": SHARED_TOKEN})
    return runtime_paths


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> GitHubPeer:
    """Route the module's GitHub client to a fake with the real client's settings."""
    peer = GitHubPeer()
    real = github_requests._github_client()

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=real.timeout,
            follow_redirects=real.follow_redirects,
            transport=httpx.MockTransport(peer.handle),
        )

    monkeypatch.setattr(github_requests, "_github_client", client)
    return peer


def test_the_github_client_follows_no_redirect_and_times_out() -> None:
    """The route's client leaves every redirect to the route and gives up on a slow GitHub."""
    real = github_requests._github_client()

    assert real.follow_redirects is False
    assert real.timeout == httpx.Timeout(30.0)


@pytest.fixture
def app(paths: RuntimePaths, github: GitHubPeer) -> FastAPI:
    """App serving the GitHub routes behind the real CORS middleware."""
    del github
    api = FastAPI(exception_handlers={RequestValidationError: validation_error_response})
    ensure_app_state(api).api_state = config_lifecycle.ApiState(
        threading.Lock(),
        config_lifecycle.ApiSnapshot(1, paths, runtime_config=Config()),
    )
    api.include_router(github_requests.router)
    api.add_middleware(_RuntimeDashboardCorsMiddleware, api_app=api, fallback_runtime_paths=paths)
    return api


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Client of the app."""
    with TestClient(app) as test_client:
        yield test_client


def session(app: FastAPI, user_id: str) -> dict[str, str]:
    """Headers presenting a new client session of ``user_id``."""
    return {"Authorization": f"Bearer {client_session_store(app).create(user_id).token}"}


def tool_context(paths: RuntimePaths, user_id: str) -> OAuthCredentialContext:
    """The connection the `github` tool of a `user_agent` agent `code` uses for ``user_id``."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=user_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    target = resolve_worker_target("user_agent", "code", execution_identity=identity)
    return resolve_oauth_credential_context(
        github_oauth_provider(),
        paths,
        get_runtime_credentials_manager(paths),
        target,
        config=Config(),
    )


def connect(
    paths: RuntimePaths,
    user_id: str,
    *,
    token: str = ALICE_TOKEN,
    expires_at: float = 4_102_444_800.0,
    login: str | None = "alice-gh",
) -> OAuthCredentialContext:
    """Store ``user_id``'s GitHub connection as Connect GitHub does, where the `github` tool reads it."""
    context = tool_context(paths, user_id)
    credentials: dict[str, object] = {
        "token": token,
        "refresh_token": "github-refresh",
        "client_id": "github-client-id",
        "token_uri": "https://github.com/login/oauth/access_token",
        "scopes": [],
        "expires_at": expires_at,
        "_source": "oauth",
        "_oauth_provider": "github",
    }
    if login is not None:
        credentials |= {"_oauth_claims_verified": True, "_oauth_claims": {"login": login}}
    publish_oauth_credentials(
        context.provider,
        credentials,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    return context


def ask(
    client: TestClient,
    auth: dict[str, str],
    path: str = "/repos/octo/hello/pulls?state=open",
    **fields: object,
) -> httpx.Response:
    """Ask the route for ``path`` with ``fields`` beside it, by default a GET, presenting the ``auth`` headers."""
    return client.post("/api/github/request", json={"method": "GET", "path": path, **fields}, headers=auth)


def test_both_routes_need_a_client_session(client: TestClient, github: GitHubPeer) -> None:
    """Without a live client session both routes answer 401 and nothing reaches GitHub."""
    assert client.get("/api/github/status").status_code == 401
    assert ask(client, {}).status_code == 401
    assert ask(client, {"Authorization": "Bearer nope"}).status_code == 401
    assert github.requests == []


def test_status_reports_the_users_own_connection_and_login(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
) -> None:
    """Each user sees only their own connection, with its verified login."""
    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {"connected": False}

    connect(paths, ALICE)

    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {
        "connected": True,
        "via": "app",
        "login": "alice-gh",
    }
    assert client.get("/api/github/status", headers=session(app, BOB)).json() == {"connected": False}


def test_status_without_a_verified_login_names_none(app: FastAPI, client: TestClient, paths: RuntimePaths) -> None:
    """A connection without a verified login reports no login."""
    connect(paths, ALICE, login=None)

    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {"connected": True, "via": "app"}


def test_status_reads_stored_state_only(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired token still counts as connected while it can be refreshed, and nothing is refreshed or fetched."""
    context = connect(paths, ALICE, token="old-token", expires_at=1.0)  # noqa: S106
    refreshes = serve_token_endpoint(monkeypatch, [])

    assert client.get("/api/github/status", headers=session(app, ALICE)).json()["connected"] is True
    assert refreshes == []
    assert github.requests == []
    assert load_oauth_credentials_snapshot_sync(context).credentials["token"] == "old-token"  # noqa: S105


def test_status_of_a_user_who_never_connected_creates_no_store(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
) -> None:
    """Asking for a user who never connected stores nothing for them."""
    before = sorted(p for p in paths.storage_root.rglob("*") if p.is_file())

    client.get("/api/github/status", headers=session(app, BOB))
    ask(client, session(app, BOB))

    assert sorted(p for p in paths.storage_root.rglob("*") if p.is_file()) == before


def test_request_goes_to_github_with_the_users_token(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A read goes to api.github.com with the user's token, and only the listed headers come back."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(
        200,
        json=[{"number": 1}],
        headers={
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-reset": "1700000000",
            "x-github-request-id": "ABCD",
            "set-cookie": "logged_in=no",
            "x-oauth-scopes": "repo",
            "link": '<https://api.github.com/repos/octo/hello/pulls?page=2>; rel="next"',
        },
    )

    response = ask(client, session(app, ALICE))

    assert response.status_code == 200
    assert response.json() == {
        "status": 200,
        "body": [{"number": 1}],
        "headers": {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1700000000", "x-github-request-id": "ABCD"},
    }
    [sent] = github.requests
    assert sent.method == "GET"
    assert str(sent.url) == "https://api.github.com/repos/octo/hello/pulls?state=open"
    assert sent.headers["authorization"] == f"Bearer {ALICE_TOKEN}"
    assert sent.headers["accept"] == DEFAULT_ACCEPT
    assert sent.headers["x-github-api-version"] == "2022-11-28"
    assert "cookie" not in sent.headers
    assert sent.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_credential_files_are_read_off_the_event_loop(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checking a connection reads the GitHub App's client file, which must not block the API's event loop."""
    connect(paths, ALICE)
    real = github_requests.oauth_credentials_usable
    on_loop: list[bool] = []

    def usable(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(github_requests, "oauth_credentials_usable", usable)

    client.get("/api/github/status", headers=session(app, ALICE))
    ask(client, session(app, ALICE))

    assert on_loop
    assert not any(on_loop)


def test_a_write_sends_its_json_body(app: FastAPI, client: TestClient, paths: RuntimePaths, github: GitHubPeer) -> None:
    """A write sends its body as JSON, and an empty answer has no body."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(204)

    response = ask(
        client,
        session(app, ALICE),
        "/repos/octo/hello/pulls/7/merge",
        method="PUT",
        body={"merge_method": "squash"},
    )

    assert response.json() == {"status": 204, "body": None, "headers": {}}
    [sent] = github.requests
    assert sent.method == "PUT"
    assert json.loads(sent.content) == {"merge_method": "squash"}
    assert sent.headers["content-type"] == "application/json"


def test_graphql_is_allowed(app: FastAPI, client: TestClient, paths: RuntimePaths, github: GitHubPeer) -> None:
    """GraphQL queries go to `/graphql` with their body."""
    connect(paths, ALICE)
    query = {"query": "query { viewer { login } }", "variables": {}}

    response = ask(client, session(app, ALICE), "/graphql", method="POST", body=query)

    assert response.json()["status"] == 200
    [sent] = github.requests
    assert str(sent.url) == "https://api.github.com/graphql"
    assert json.loads(sent.content) == query


def test_a_diff_answers_with_text(app: FastAPI, client: TestClient, paths: RuntimePaths, github: GitHubPeer) -> None:
    """A diff is requested with its media type and answered as text."""
    connect(paths, ALICE)
    diff = "diff --git a/x b/x\n+added\n"
    github.handler = lambda _request: httpx.Response(200, text=diff, headers={"content-type": "text/plain"})

    response = ask(client, session(app, ALICE), "/repos/octo/hello/pulls/7", accept="application/vnd.github.diff")

    assert response.json()["body"] == diff
    assert github.requests[0].headers["accept"] == "application/vnd.github.diff"


def test_an_error_answer_to_a_diff_request_is_json(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """The answer's own media type decides the body, so GitHub's JSON error to a diff request is an object."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(
        404,
        content=b'{"message":"Not Found"}',
        headers={"content-type": "application/json; charset=utf-8"},
    )

    response = ask(client, session(app, ALICE), "/repos/octo/hello/pulls/7", accept="application/vnd.github.diff")

    assert response.json()["body"] == {"message": "Not Found"}


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"number": 1}', "text/plain"),
        (b"not json", "application/json"),
        (b'{"x": NaN}', "application/json"),
        (b"[Infinity]", "application/vnd.github+json"),
        (b"[1e999]", "application/json"),
    ],
)
def test_an_answer_that_is_not_json_is_text(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    content: bytes,
    content_type: str,
) -> None:
    """An answer GitHub does not mark as JSON, or that is not strict JSON, comes back as its text."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(200, content=content, headers={"content-type": content_type})

    response = ask(client, session(app, ALICE))

    assert response.status_code == 200
    assert response.json()["body"] == content.decode()


def test_githubs_refusal_is_answered_as_its_status(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """GitHub's refusal is a 200 here, carrying GitHub's status, body, and retry header."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(
        403,
        json={"message": "Resource not accessible by integration"},
        headers={"retry-after": "60"},
    )

    response = ask(client, session(app, ALICE), "/repos/octo/hello/pulls/7/merge", method="PUT", body={})

    assert response.status_code == 200
    assert response.json() == {
        "status": 403,
        "body": {"message": "Resource not accessible by integration"},
        "headers": {"retry-after": "60"},
    }


def test_without_a_connection_nothing_is_sent_even_with_shared_tokens(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """Bob gets neither Alice's account nor the shared tokens the environment and the `github` tool hold."""
    connect(paths, ALICE)

    response = ask(client, session(app, BOB))

    assert response.status_code == 409
    assert response.json() == {"error": "github_not_connected"}
    assert github.requests == []


def test_a_session_without_a_user_never_reaches_the_global_connection(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A session naming no user resolves no person's connection, so the unscoped store's account is never used."""
    provider = github_oauth_provider()
    publish_oauth_credentials(
        provider,
        {
            "token": "gho-global",
            "refresh_token": "global-refresh",
            "client_id": "github-client-id",
            "token_uri": "https://github.com/login/oauth/access_token",
            "scopes": [],
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=None,
    )

    assert client.get("/api/github/status", headers=session(app, "")).json() == {"connected": False}
    assert ask(client, session(app, "")).json() == {"error": "github_not_connected"}
    assert github.requests == []


def test_without_an_oauth_client_the_user_is_not_connected(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """Without the GitHub App's client a stored connection counts as not connected."""
    connect(paths, ALICE)
    get_runtime_credentials_manager(paths).delete_credentials("github_oauth_client")

    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {"connected": False}
    assert ask(client, session(app, ALICE)).status_code == 409
    assert github.requests == []


def test_an_unreadable_connection_is_not_connected(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A connection that cannot be read counts as not connected."""
    connect(paths, ALICE)
    database = next(paths.storage_root.rglob("github_oauth_credentials.sqlite3"))
    corrupt_oauth_credential_payload(database, b"not-a-payload")

    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {"connected": False}
    assert ask(client, session(app, ALICE)).json() == {"error": "github_not_connected"}
    assert github.requests == []


def test_an_expiring_token_is_refreshed_first(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired token is refreshed through the token endpoint before the request goes out."""
    connect(paths, ALICE, token="old-token", expires_at=1.0)  # noqa: S106
    refreshes = serve_token_endpoint(
        monkeypatch,
        [
            httpx.Response(
                200,
                json={
                    "access_token": "gho-rotated",
                    "refresh_token": "rotated-refresh",
                    "token_type": "bearer",
                    "expires_in": 28800,
                },
            ),
        ],
    )

    assert ask(client, session(app, ALICE)).json()["status"] == 200

    assert refreshes == ["github-refresh"]
    assert github.requests[0].headers["authorization"] == "Bearer gho-rotated"


def test_a_rejected_refresh_is_not_connected(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh GitHub rejects means the user must connect again."""
    connect(paths, ALICE, token="old-token", expires_at=1.0)  # noqa: S106
    serve_token_endpoint(monkeypatch, [httpx.Response(400, json={"error": "invalid_grant"})])

    assert ask(client, session(app, ALICE)).json() == {"error": "github_not_connected"}
    assert github.requests == []


def test_a_failed_refresh_can_be_retried(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that fails for another reason answers 503, which a retry may fix."""
    connect(paths, ALICE, token="old-token", expires_at=1.0)  # noqa: S106
    serve_token_endpoint(monkeypatch, [httpx.Response(503, text="unavailable")])

    response = ask(client, session(app, ALICE))

    assert response.status_code == 503
    assert response.json() == {"error": "github_connection_unavailable"}
    assert github.requests == []


def test_a_connection_store_that_fails_can_be_retried(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the stored connection cannot be read for a passing reason, both routes answer 503."""
    connect(paths, ALICE)

    async def fail(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "store busy"
        raise OAuthProviderError(msg)

    monkeypatch.setattr(github_requests, "load_oauth_credentials_status", fail)

    status = client.get("/api/github/status", headers=session(app, ALICE))
    request = ask(client, session(app, ALICE))

    assert (status.status_code, status.json()) == (503, {"error": "github_connection_unavailable"})
    assert (request.status_code, request.json()) == (503, {"error": "github_connection_unavailable"})
    assert github.requests == []


def test_a_redirect_on_api_github_com_is_followed_with_the_token(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A renamed repository's redirect to `/repositories/<id>` is followed, and its answer is the one relayed."""
    connect(paths, ALICE)

    def moved(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/"):
            return httpx.Response(301, headers={"location": "https://api.github.com/repositories/1/pulls/1"})
        return httpx.Response(200, json={"number": 1}, headers={"x-github-request-id": "EF"})

    github.handler = moved

    with capture_logs() as logs:
        response = ask(client, session(app, ALICE), "/repos/octo/old-name/pulls/1")

    assert response.json() == {"status": 200, "body": {"number": 1}, "headers": {"x-github-request-id": "EF"}}
    first, followed = github.requests
    assert str(followed.url) == "https://api.github.com/repositories/1/pulls/1"
    assert followed.method == "GET"
    for name in ("authorization", "accept", "x-github-api-version"):
        assert followed.headers[name] == first.headers[name]
    assert followed.headers["authorization"] == f"Bearer {ALICE_TOKEN}"
    assert {"path": "/repos/{owner}/{repo}/pulls", "status": 200, "redirects": 1}.items() <= logs[-1].items()


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/steal",
        "//evil.example/repos/octo/hello",
        "http://api.github.com/repositories/1/pulls/1",
        "https://api.github.com:8443/repositories/1/pulls/1",
        "https://user@api.github.com/repositories/1/pulls/1",
        "https://user:pass@api.github.com/repositories/1/pulls/1",
        "https://api.github.com.evil.example/repositories/1",
        "https://api.github.com@evil.example/repositories/1",
        None,
    ],
)
def test_a_redirect_off_api_github_com_is_answered_not_followed(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    location: str | None,
) -> None:
    """A redirect anywhere but `https://api.github.com` is answered as GitHub's status, so the token never follows it."""
    connect(paths, ALICE)
    headers = {} if location is None else {"location": location}
    github.handler = lambda _request: httpx.Response(302, headers=headers)

    response = ask(client, session(app, ALICE))

    assert response.json() == {"status": 302, "body": None, "headers": {}}
    assert len(github.requests) == 1


def test_a_redirect_loop_stops_after_three_hops(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """After three followed redirects the next one is answered as GitHub's status."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(308, headers={"location": "/repositories/1"})

    response = ask(client, session(app, ALICE))

    assert response.json() == {"status": 308, "body": None, "headers": {}}
    assert len(github.requests) == 4


@pytest.mark.parametrize(("method", "status"), [("POST", 301), ("PATCH", 302), ("DELETE", 301)])
def test_a_write_is_not_resent_on_a_moved_redirect(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    method: str,
    status: int,
) -> None:
    """A 301 or 302 answer to a write is relayed, so the write is never sent again by surprise."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(status, headers={"location": "/repositories/1/issues/3/comments"})

    response = ask(client, session(app, ALICE), "/repos/octo/hello/issues/3/comments", method=method, body={"b": 1})

    assert response.json()["status"] == status
    assert len(github.requests) == 1


@pytest.mark.parametrize("status", [307, 308])
def test_a_write_follows_a_redirect_that_keeps_its_method(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    status: int,
) -> None:
    """A 307 or 308 answer to a write sends the same method and body to the new URL."""
    connect(paths, ALICE)

    def moved(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/"):
            return httpx.Response(status, headers={"location": "/repositories/1/pulls/7/merge"})
        return httpx.Response(200, json={"merged": True})

    github.handler = moved

    response = ask(client, session(app, ALICE), "/repos/octo/hello/pulls/7/merge", method="PUT", body={"m": "squash"})

    assert response.json()["body"] == {"merged": True}
    _, followed = github.requests
    assert (followed.method, str(followed.url)) == ("PUT", "https://api.github.com/repositories/1/pulls/7/merge")
    assert json.loads(followed.content) == {"m": "squash"}
    assert followed.headers["content-type"] == "application/json"
    assert followed.headers["authorization"] == f"Bearer {ALICE_TOKEN}"


def test_a_see_other_redirect_is_fetched_with_a_get(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A 303 answer to a write is followed by a GET without the body."""
    connect(paths, ALICE)

    def moved(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/"):
            return httpx.Response(303, headers={"location": "/repositories/1/issues/3"})
        return httpx.Response(200, json={"number": 3})

    github.handler = moved

    response = ask(client, session(app, ALICE), "/repos/octo/hello/issues/3", method="PATCH", body={"state": "open"})

    assert response.json()["body"] == {"number": 3}
    _, followed = github.requests
    assert (followed.method, followed.content) == ("GET", b"")
    assert "content-type" not in followed.headers
    assert followed.headers["authorization"] == f"Bearer {ALICE_TOKEN}"


def test_redirects_share_one_deadline(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout bounds the whole exchange, so redirects that are each fast enough still give up together."""
    connect(paths, ALICE)
    monkeypatch.setattr(github_requests, "_TIMEOUT_SECONDS", 0.1)

    async def slow_redirect(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.04)
        return httpx.Response(307, headers={"location": "/repositories/1"})

    github.handler = slow_redirect

    response = ask(client, session(app, ALICE))

    assert (response.status_code, response.json()) == (502, {"error": "github_unreachable"})


def test_an_oversized_answer_is_refused(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer over the size cap is refused rather than relayed."""
    connect(paths, ALICE)
    monkeypatch.setattr(github_requests, "_MAX_RESPONSE_BYTES", 1024)
    github.handler = lambda _request: httpx.Response(200, content=b"x" * 1025)

    response = ask(client, session(app, ALICE))

    assert response.status_code == 502
    assert response.json() == {"error": "github_response_too_large"}


def test_an_unreachable_github_is_a_bad_gateway(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A timeout or network failure answers 502."""
    connect(paths, ALICE)

    def time_out(request: httpx.Request) -> httpx.Response:
        msg = "slow"
        raise httpx.ReadTimeout(msg, request=request)

    github.handler = time_out

    response = ask(client, session(app, ALICE))

    assert response.status_code == 502
    assert response.json() == {"error": "github_unreachable"}


@pytest.mark.parametrize(
    "path",
    [
        "/user",
        "/user/repos",
        "/orgs/octo/repos",
        "/search/code?q=secret",
        "/repos/octo",
        "/repos/octo/",
        "/repos/octo/hello/",
        "/repos//hello",
        "/repos/octo//pulls",
        "/repos/octo/hello//pulls",
        "//evil.example/repos/octo/hello",
        "https://evil.example/repos/octo/hello",
        "http://api.github.com/repos/octo/hello",
        "api.github.com/repos/octo/hello",
        "repos/octo/hello",
        "",
        "/repos/octo/hello/../../../user",
        "/repos/octo/hello/./pulls",
        "/repos/octo/..",
        "/repos/../user/x",
        "/repos/octo/hello/%2e%2e/%2e%2e/%2e%2e/user",
        "/repos/octo/hello/%2E%2E",
        "/repos/octo/hello/.%2e",
        "/repos/octo/hello%2f..%2f..%2fuser",
        "/repos/octo/hello/%5c..%5cuser",
        "/repos/octo/hello/%",
        "/repos/octo/hello/%zz",
        "/repos/octo/hello\\..\\user",
        "/repos/octo/hello/pulls\n",
        "/repos/octo/hello/pulls\r\nHost: evil.example",
        "/repos/octo/hello/\x00",
        "/repos/octo/hello/%00",
        "/repos/octo/hello/%0d%0aHost:%20evil",
        "/repos/octo/hello/\x7f",
        "/repos/octo/hello/pulls?state=open\n",
        "/repos/octo/hello pulls",
        "/repos/octo/hello/pulls#x",
        "/repos/octo/hello/é",
        "/graphql/../user",
        "/graphqlx",
        "/graphql/",
        "/GRAPHQL",
        "/Repos/octo/hello",
    ],
)
def test_paths_outside_repos_and_graphql_are_refused(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    path: str,
) -> None:
    """Paths outside a repository and GraphQL, or that could resolve outside one, answer 400."""
    connect(paths, ALICE)

    response = ask(client, session(app, ALICE), path)

    assert response.status_code == 400
    assert response.json() == {"error": "github_path_not_allowed"}
    assert github.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/repos/octo/hello/" + "a" * 70_000,
        "/repos/octo/hello/pulls?q=" + "a" * 70_000,
        "/repos/octo/hello/" + "a" * 4_079,
    ],
    ids=["past the URL limit", "query past the URL limit", "4097 characters"],
)
def test_overlong_paths_are_refused(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    path: str,
) -> None:
    """A path longer than 4096 characters answers 400, even one too long for a URL."""
    connect(paths, ALICE)

    response = ask(client, session(app, ALICE), path)

    assert response.status_code == 400
    assert response.json() == {"error": "github_path_not_allowed"}
    assert github.requests == []


def test_a_path_of_4096_characters_is_sent(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """The longest path a client may ask for still reaches GitHub."""
    connect(paths, ALICE)
    path = "/repos/octo/hello/" + "a" * (4096 - len("/repos/octo/hello/"))

    assert ask(client, session(app, ALICE), path).json()["status"] == 200
    assert github.requests[0].url.raw_path.decode() == path


@pytest.mark.parametrize(
    "path",
    [
        "/repos/octo/hello",
        "/repos/octo/hello.js/pulls/7/files?per_page=100&page=2",
        "/repos/octo/hello/compare/main...feature",
        "/repos/octo/hello/git/refs/heads/feat/x",
        "/repos/octo/hello/contents/docs/a%20b.md?ref=main",
        "/repos/octo/hello/commits?since=2026-01-01T00:00:00Z",
        "/graphql",
    ],
)
def test_repository_paths_are_sent_unchanged(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    path: str,
) -> None:
    """Repository paths, dots and encoded characters included, reach api.github.com as given."""
    connect(paths, ALICE)

    assert ask(client, session(app, ALICE), path).json()["status"] == 200

    [sent] = github.requests
    assert sent.url.host == "api.github.com"
    assert sent.url.raw_path.decode() == path


@pytest.mark.parametrize(
    "fields",
    [
        {"method": "HEAD"},
        {"method": "OPTIONS"},
        {"method": "TRACE"},
        {"method": "get"},
        {"accept": "text/html"},
        {"accept": "application/vnd.github.raw"},
        {"headers": {"Authorization": "Bearer other"}},
        {"url": "https://evil.example"},
    ],
)
def test_other_methods_accepts_and_fields_are_refused(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    fields: dict[str, object],
) -> None:
    """Other methods, media types, and any extra field are refused before anything is sent."""
    connect(paths, ALICE)

    response = ask(client, session(app, ALICE), **fields)

    assert response.status_code == 422
    assert github.requests == []


def test_logs_name_the_call_but_never_its_token_or_bodies(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """Logs carry the method, the path's kind, and the status, never the token, the repository, or a body."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(201, json={"answer": "response-body-secret"})

    with capture_logs() as logs:
        ask(
            client,
            session(app, ALICE),
            "/repos/octo/hello/issues/3/comments?q=query-secret",
            method="POST",
            body={"body": "request-body-secret"},
        )

    assert {"method": "POST", "path": "/repos/{owner}/{repo}/issues", "status": 201}.items() <= logs[-1].items()
    logged = repr(logs)
    for secret in (ALICE_TOKEN, "request-body-secret", "response-body-secret", "query-secret", "octo", "hello"):
        assert secret not in logged


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/repos/octo/hello", "/repos/{owner}/{repo}"),
        ("/repos/octo/hello/check-runs/9", "/repos/{owner}/{repo}/check-runs"),
        ("/repos/octo/hello/git/refs/heads/x", "/repos/{owner}/{repo}/git"),
        ("/repos/octo/hello/level%3Dcritical%20event%3Dfake", "/repos/{owner}/{repo}/{other}"),
        ("/repos/octo/hello/" + "%41" * 1000, "/repos/{owner}/{repo}/{other}"),
        ("/repos/octo/hello/" + "a" * 33, "/repos/{owner}/{repo}/{other}"),
        ("/repos/octo/hello/Pulls", "/repos/{owner}/{repo}/{other}"),
        ("/graphql", "/graphql"),
    ],
)
def test_logs_name_only_a_plain_resource(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    path: str,
    kind: str,
) -> None:
    """The segment after the repository is logged only when it reads as an API resource name."""
    connect(paths, ALICE)

    with capture_logs() as logs:
        ask(client, session(app, ALICE), path)

    assert logs[-1]["path"] == kind


def test_a_client_on_an_allowed_origin_may_call_the_routes(client: TestClient) -> None:
    """MindRoom Chat's origin may call the routes without credentials, and other origins may not."""
    preflight = client.options(
        "/api/github/request",
        headers={
            "Origin": CLIENT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == CLIENT_ORIGIN
    assert "access-control-allow-credentials" not in preflight.headers
    status = client.get("/api/github/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in status.headers


def user_answer(login: str = "pat-gh") -> Handler:
    """Answer `GET /user` as GitHub does for a token of ``login``, and anything else with a JSON 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": login, "id": 1, "email": "login-lookup-secret@example.org"})
        return httpx.Response(200, json={"ok": True})

    return handler


def save_token(client: TestClient, auth: dict[str, str], token: object = ALICE_PAT) -> httpx.Response:
    """Save ``token`` as the session user's GitHub token."""
    return client.put("/api/github/token", json={"token": token}, headers=auth)


def stored_token(paths: RuntimePaths, user_id: str) -> dict[str, Any] | None:
    """The token document kept in ``user_id``'s own primary-only store, the one their GitHub App connection uses."""
    manager = get_runtime_credentials_manager(paths).for_primary_runtime_scope(user_id, None)
    return manager.load_credentials("github_user_token")


def test_a_saved_token_is_checked_with_github_and_stored_for_the_user_only(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """GitHub confirms the token's login before it is stored in the user's own store, never the shared ones."""
    github.handler = user_answer()

    response = save_token(client, session(app, ALICE))

    assert (response.status_code, response.json()) == (200, {"connected": True, "via": "token", "login": "pat-gh"})
    [check] = github.requests
    assert (check.method, str(check.url)) == ("GET", "https://api.github.com/user")
    assert check.headers["authorization"] == f"Bearer {ALICE_PAT}"
    assert check.headers["accept"] == DEFAULT_ACCEPT
    assert stored_token(paths, ALICE) == {"token": ALICE_PAT, "login": "pat-gh"}
    manager = get_runtime_credentials_manager(paths)
    assert manager.load_credentials("github") == {"access_token": SHARED_TOKEN}
    assert manager.load_credentials("github_user_token") is None
    assert load_oauth_credentials_snapshot_sync(tool_context(paths, ALICE)).credentials is None
    assert client.get("/api/github/status", headers=session(app, BOB)).json() == {"connected": False}


def test_a_saved_token_is_used_without_the_github_app(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A token works on its own, even with no GitHub App client saved, and its 401 is relayed with the token kept."""
    get_runtime_credentials_manager(paths).delete_credentials("github_oauth_client")
    github.handler = user_answer()
    save_token(client, session(app, ALICE))
    github.handler = lambda _request: httpx.Response(401, json={"message": "Bad credentials"})

    status = client.get("/api/github/status", headers=session(app, ALICE))
    response = ask(client, session(app, ALICE))

    assert status.json() == {"connected": True, "via": "token", "login": "pat-gh"}
    assert response.json() == {"status": 401, "body": {"message": "Bad credentials"}, "headers": {}}
    assert github.requests[-1].headers["authorization"] == f"Bearer {ALICE_PAT}"
    assert stored_token(paths, ALICE) == {"token": ALICE_PAT, "login": "pat-gh"}


def test_a_token_wins_over_the_app_connection_until_it_is_removed(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """The token answers while it is saved, and removing it falls back to the GitHub App connection."""
    connect(paths, ALICE)
    github.handler = user_answer()
    save_token(client, session(app, ALICE))

    status = client.get("/api/github/status", headers=session(app, ALICE))
    ask(client, session(app, ALICE))
    removed = client.delete("/api/github/token", headers=session(app, ALICE))
    ask(client, session(app, ALICE))
    removed_again = client.delete("/api/github/token", headers=session(app, ALICE))

    assert status.json() == {"connected": True, "via": "token", "login": "pat-gh", "app_connected": True}
    assert [r.headers["authorization"] for r in github.requests[1:]] == [f"Bearer {ALICE_PAT}", f"Bearer {ALICE_TOKEN}"]
    assert (removed.status_code, removed.json()) == (200, {"connected": True, "via": "app", "login": "alice-gh"})
    assert (removed_again.status_code, removed_again.json()) == (200, removed.json())
    assert stored_token(paths, ALICE) is None


def test_removing_the_only_connection_leaves_none(
    app: FastAPI,
    client: TestClient,
    github: GitHubPeer,
) -> None:
    """Removing a token without an App connection behind it answers not connected, and removing none is fine."""
    github.handler = user_answer()
    save_token(client, session(app, ALICE))

    assert client.delete("/api/github/token", headers=session(app, ALICE)).json() == {"connected": False}
    assert client.delete("/api/github/token", headers=session(app, BOB)).json() == {"connected": False}
    assert ask(client, session(app, ALICE)).status_code == 409


def test_a_token_is_used_when_the_app_connection_cannot_be_read(
    app: FastAPI,
    client: TestClient,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store failure of the GitHub App connection does not hide a saved token."""
    github.handler = user_answer()
    save_token(client, session(app, ALICE))

    async def fail(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "store busy"
        raise OAuthProviderError(msg)

    monkeypatch.setattr(github_requests, "load_oauth_credentials_status", fail)

    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {
        "connected": True,
        "via": "token",
        "login": "pat-gh",
    }
    assert ask(client, session(app, ALICE)).json()["status"] == 200


@pytest.mark.parametrize("status_code", [401, 403])
def test_a_token_github_refuses_is_not_stored(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    status_code: int,
) -> None:
    """GitHub's 401 or 403 answers 400 and keeps whatever was saved before."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(status_code, json={"message": "Bad credentials"})

    response = save_token(client, session(app, ALICE))

    assert (response.status_code, response.json()) == (400, {"error": "github_token_rejected"})
    assert stored_token(paths, ALICE) is None
    assert client.get("/api/github/status", headers=session(app, ALICE)).json()["via"] == "app"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"message": "Server Error"}),
        httpx.Response(502),
        httpx.Response(301, headers={"location": "https://evil.example/user"}),
        httpx.Response(200, json={"id": 1}),
        httpx.Response(200, text="not json"),
    ],
)
def test_a_token_check_without_a_login_is_a_bad_gateway(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    answer: httpx.Response,
) -> None:
    """A failing GitHub, a redirect, which is never followed, or an answer naming no login stores nothing."""
    github.handler = lambda _request: answer

    response = save_token(client, session(app, ALICE))

    assert (response.status_code, response.json()) == (502, {"error": "github_unreachable"})
    assert len(github.requests) == 1
    assert stored_token(paths, ALICE) is None


def test_an_unreachable_or_slow_github_stores_no_token(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network failure or a check slower than its timeout answers 502."""
    assert github_requests._TOKEN_CHECK_TIMEOUT_SECONDS == 10.0

    def fail(request: httpx.Request) -> httpx.Response:
        msg = "down"
        raise httpx.ConnectError(msg, request=request)

    github.handler = fail
    unreachable = save_token(client, session(app, ALICE))
    monkeypatch.setattr(github_requests, "_TOKEN_CHECK_TIMEOUT_SECONDS", 0.05)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"login": "pat-gh"})

    github.handler = slow
    timed_out = save_token(client, session(app, ALICE))

    assert (unreachable.status_code, unreachable.json()) == (502, {"error": "github_unreachable"})
    assert (timed_out.status_code, timed_out.json()) == (502, {"error": "github_unreachable"})
    assert stored_token(paths, ALICE) is None


@pytest.mark.parametrize(
    "token",
    ["", "a" * 513, "ghp_with space", "ghp_tab\t", "ghp_newline\n", "ghp_nul\x00", "ghp_del\x7f", "ghp_é", 5, None],
    ids=["empty", "513-chars", "space", "tab", "newline", "nul", "del", "non-ascii", "number", "null"],
)
def test_a_malformed_token_is_refused_unsent_and_unechoed(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    token: object,
) -> None:
    """A token that is empty, over 512 characters, or holds a space or control character answers 422."""
    response = save_token(client, session(app, ALICE), token)

    assert response.status_code == 422
    if isinstance(token, str) and token:
        assert token not in response.text
    assert github.requests == []
    assert stored_token(paths, ALICE) is None


def test_a_token_of_512_characters_is_accepted(app: FastAPI, client: TestClient, github: GitHubPeer) -> None:
    """The longest token allowed is checked with GitHub."""
    github.handler = user_answer()

    assert save_token(client, session(app, ALICE), "a" * 512).status_code == 200


def test_a_session_without_a_user_cannot_save_a_token(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A session naming no user has no store of its own, so nothing is checked or stored, not even globally."""
    github.handler = user_answer()
    before = sorted(p for p in paths.storage_root.rglob("*") if p.is_file())

    saved = save_token(client, session(app, ""))
    removed = client.delete("/api/github/token", headers=session(app, ""))

    assert (saved.status_code, saved.json()) == (409, {"error": "github_not_connected"})
    assert (removed.status_code, removed.json()) == (200, {"connected": False})
    assert github.requests == []
    assert sorted(p for p in paths.storage_root.rglob("*") if p.is_file()) == before


def test_the_token_routes_need_a_client_session(client: TestClient, github: GitHubPeer) -> None:
    """Without a live client session saving and removing a token answer 401 and nothing reaches GitHub."""
    assert save_token(client, {}).status_code == 401
    assert client.delete("/api/github/token").status_code == 401
    assert github.requests == []


def test_without_a_token_nothing_is_sent_even_with_another_users_token_or_shared_tokens(
    app: FastAPI,
    client: TestClient,
    github: GitHubPeer,
) -> None:
    """Bob gets neither Alice's saved token nor the shared tokens the environment and the `github` tool hold."""
    github.handler = user_answer()
    save_token(client, session(app, ALICE))

    response = ask(client, session(app, BOB))

    assert (response.status_code, response.json()) == (409, {"error": "github_not_connected"})
    assert len(github.requests) == 1


def test_logs_never_carry_a_token_or_its_login_lookup(
    app: FastAPI,
    client: TestClient,
    github: GitHubPeer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Saving, failing to save, and using a token log neither the token nor what GitHub said about it."""
    caplog.set_level("DEBUG")
    with capture_logs() as logs:
        github.handler = lambda _request: httpx.Response(401, json={"message": "Bad credentials"})
        save_token(client, session(app, ALICE), "ghp_rejected-token")
        save_token(client, session(app, ALICE), "ghp_bad token")
        github.handler = user_answer()
        save_token(client, session(app, ALICE))
        ask(client, session(app, ALICE))

    logged = repr(logs) + caplog.text
    for secret in (
        ALICE_PAT,
        "ghp_rejected-token",
        "ghp_bad token",
        "pat-gh",
        "login-lookup-secret",
        "Bad credentials",
    ):
        assert secret not in logged


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/github/token", {"token": "ghp_value secret"}),
        ("/api/github/token", {"ghp_key-secret": "x"}),
        ("/api/github/token", {"token": ALICE_PAT, "ghp_key-secret": "x"}),
        ("/api/github/token", ["ghp_value-secret"]),
        ("/api/github/request", {"method": "GET", "path": "/graphql", "ghp_key-secret": "x"}),
        ("/api/github/request", {"method": "ghp_value-secret", "path": "/graphql"}),
    ],
)
def test_the_real_app_answers_a_malformed_body_without_echoing_it(
    test_client: TestClient,
    path: str,
    body: object,
) -> None:
    """The mounted routes answer a fixed 422 that names neither a rejected value nor a rejected key."""
    from mindroom.api import main  # noqa: PLC0415

    auth = {"Authorization": f"Bearer {client_session_store(main.app).create(ALICE).token}"}
    method = "PUT" if path.endswith("/token") else "POST"

    response = test_client.request(method, path, json=body, headers=auth)

    assert (response.status_code, response.json()) == (422, {"error": "github_request_invalid"})
    assert "secret" not in response.text


def damage_token(paths: RuntimePaths, user_id: str, payload: bytes) -> None:
    """Leave ``payload`` where ``user_id``'s saved token lives, as a damaged file or one under another key would."""
    manager = get_runtime_credentials_manager(paths).for_primary_runtime_scope(user_id, None)
    manager.save_credentials("github_user_token", {"token": "ghp_old", "login": "old-gh"})
    manager.get_credentials_path("github_user_token").write_bytes(payload)


DAMAGED_TOKENS = pytest.mark.parametrize(
    "payload",
    [b"not json", b"MINDROOM-CREDENTIALS-V1\nsealed", b'["ghp_listed"]', b'{"token": 5, "login": "x"}', b"{}"],
    ids=["broken", "encrypted-without-key", "not-an-object", "token-not-text", "empty-object"],
)


@DAMAGED_TOKENS
def test_an_unreadable_saved_token_never_falls_back_to_the_app_connection(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    payload: bytes,
) -> None:
    """Losing a saved token is no request to switch accounts: status and requests answer 503 and nothing is sent."""
    connect(paths, ALICE)
    damage_token(paths, ALICE, payload)

    status = client.get("/api/github/status", headers=session(app, ALICE))
    response = ask(client, session(app, ALICE), method="POST", body={"title": "x"})

    assert (status.status_code, status.json()) == (503, {"error": "github_connection_unavailable"})
    assert (response.status_code, response.json()) == (503, {"error": "github_connection_unavailable"})
    assert github.requests == []


@DAMAGED_TOKENS
def test_an_unreadable_saved_token_can_be_replaced_or_removed(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
    payload: bytes,
) -> None:
    """A new token replaces an unreadable one, and removing it leaves the GitHub App connection."""
    connect(paths, ALICE)
    github.handler = user_answer()
    damage_token(paths, ALICE, payload)

    saved = save_token(client, session(app, ALICE))
    ask(client, session(app, ALICE))
    damage_token(paths, ALICE, payload)
    removed = client.delete("/api/github/token", headers=session(app, ALICE))

    assert (saved.status_code, saved.json()) == (200, {"connected": True, "via": "token", "login": "pat-gh"})
    assert github.requests[-1].headers["authorization"] == f"Bearer {ALICE_PAT}"
    assert (removed.status_code, removed.json()) == (200, {"connected": True, "via": "app", "login": "alice-gh"})
    assert stored_token(paths, ALICE) is None


class CountedStream(httpx.AsyncByteStream):
    """A response body that counts how many of its chunks were read."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield each chunk, counting it."""
        for chunk in self.chunks:
            self.read += 1
            yield chunk


@pytest.mark.parametrize("status_code", [401, 403, 404, 500])
def test_a_token_check_reads_no_body_of_a_failed_answer(
    app: FastAPI,
    client: TestClient,
    github: GitHubPeer,
    status_code: int,
) -> None:
    """GitHub's status alone decides a failed check, so its body is never downloaded."""
    body = CountedStream(b'{"message": "Bad credentials"}')
    github.handler = lambda _request: httpx.Response(
        status_code, stream=body, headers={"content-type": "application/json"}
    )

    assert save_token(client, session(app, ALICE)).status_code in {400, 502}
    assert body.read == 0


def test_a_token_check_answer_past_its_limit_is_a_bad_gateway(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A `/user` answer over 64 KiB is not read to its end, and nothing is stored."""
    body = CountedStream(b'{"login": "pat-gh", "pad": "', b"a" * 65_536, b'"}', b" " * 65_536)
    github.handler = lambda _request: httpx.Response(200, stream=body, headers={"content-type": "application/json"})

    response = save_token(client, session(app, ALICE))

    assert (response.status_code, response.json()) == (502, {"error": "github_unreachable"})
    assert body.read == 2
    assert stored_token(paths, ALICE) is None

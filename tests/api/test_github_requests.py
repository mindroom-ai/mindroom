"""A signed-in client's GitHub requests go out with its user's own connected GitHub account, which stays here."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from mindroom.api import config_lifecycle, github_requests
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.api.main import _RuntimeDashboardCorsMiddleware
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import load_oauth_credentials_snapshot_sync, resolve_oauth_credential_context
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthProviderError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import corrupt_oauth_credential_payload, publish_oauth_credentials, serve_token_endpoint

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
CLIENT_ORIGIN = "https://chat.example.org"
ALICE_TOKEN = "gho-alice-access"  # noqa: S105
SHARED_TOKEN = "ghp-shared-token"  # noqa: S105
DEFAULT_ACCEPT = "application/vnd.github+json"

type Handler = Callable[[httpx.Request], httpx.Response]


class GitHubPeer:
    """Fake api.github.com that records each request and answers with ``handler``, by default a JSON 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda _request: httpx.Response(200, json={"ok": True})

    def handle(self, request: httpx.Request) -> httpx.Response:
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
    """The route's client sends the token to one URL only and gives up on a slow GitHub."""
    real = github_requests._github_client()

    assert real.follow_redirects is False
    assert real.timeout == httpx.Timeout(30.0)


@pytest.fixture
def app(paths: RuntimePaths, github: GitHubPeer) -> FastAPI:
    """App serving the GitHub routes behind the real CORS middleware."""
    del github
    api = FastAPI()
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
        "login": "alice-gh",
    }
    assert client.get("/api/github/status", headers=session(app, BOB)).json() == {"connected": False}


def test_status_without_a_verified_login_names_none(app: FastAPI, client: TestClient, paths: RuntimePaths) -> None:
    """A connection without a verified login reports no login."""
    connect(paths, ALICE, login=None)

    assert client.get("/api/github/status", headers=session(app, ALICE)).json() == {"connected": True}


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


def test_a_redirect_is_answered_not_followed(
    app: FastAPI,
    client: TestClient,
    paths: RuntimePaths,
    github: GitHubPeer,
) -> None:
    """A redirect is answered as GitHub's status, so the token never follows it."""
    connect(paths, ALICE)
    github.handler = lambda _request: httpx.Response(302, headers={"location": "https://evil.example/steal"})

    response = ask(client, session(app, ALICE))

    assert response.json() == {"status": 302, "body": None, "headers": {}}
    assert len(github.requests) == 1


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

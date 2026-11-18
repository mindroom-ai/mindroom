"""`/api/github`: GitHub API requests a signed-in client makes with its user's own GitHub account.

The account is a personal access token the user saved here, else their GitHub App connection (Connect GitHub).
The client names a method and a repository or GraphQL path; this route adds the user's token, sends the request to
`api.github.com` only, and answers with GitHub's status, body, and a few headers, so the token never leaves MindRoom.
This route's own log lines carry the method, the path's kind, and GitHub's status, never the token, the repository, or
a body; HTTP client libraries may log the URL at INFO or DEBUG, as for every outbound call MindRoom makes.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, override
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mindroom.api import config_lifecycle
from mindroom.api.client_sessions import presented_session
from mindroom.credentials.manager import (
    CredentialsUnreadableError,
    credential_layers,
    get_runtime_credentials_manager,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    load_oauth_credentials_status,
    oauth_credentials_usable,
    oauth_verified_claim,
    refresh_oauth_credentials,
    resolve_oauth_credential_context,
)
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthProviderError, OAuthRefreshRejectedError
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from fastapi import Response

    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext

logger = get_logger(__name__)


class _UnechoedValidationRoute(APIRoute):
    """A route answering a malformed body with one fixed error.

    The app's 422 body leaves out rejected values but names rejected keys, and a token sent as a key is still a token.
    """

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                return _error(422, "github_request_invalid")

        return route


router = APIRouter(prefix="/api/github", route_class=_UnechoedValidationRoute)

_GITHUB_API = "https://api.github.com"
_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# GitHub redirects a renamed or transferred repository's old path to `/repositories/<id>/...` on the same host.
_MAX_REDIRECTS = 3
# Far longer than any GitHub API path a client builds, and far below the longest URL httpx sends.
_MAX_PATH_LENGTH = 4096
# The stored connection could not be read or refreshed for a reason a retry may fix.
_UNAVAILABLE = "github_connection_unavailable"
_PASSED_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-reset", "retry-after", "x-github-request-id")
# One RFC 3986 path character, a `%` always followed by two hex digits. This leaves out controls, spaces, `\`, `#`,
# and anything outside ASCII, and since every segment is non-empty, `//`, so no path can name another host.
_PCHAR = r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})"
# Logs name the segment after the repository only when it reads as an API resource, such as `pulls` or `check-runs`.
_RESOURCE = re.compile(r"[a-z][a-z_-]{0,31}")
_PATH = re.compile(rf"(/graphql|/repos(?:/{_PCHAR}+){{2,}})(?:\?(?:{_PCHAR}|[/?])*)?")
# The user's own personal access token, beside their GitHub App connection; never the `github` tool's shared token.
_TOKEN_SERVICE = "github_user_token"  # noqa: S105
_TOKEN_CHECK_TIMEOUT_SECONDS = 10.0
# GitHub's `/user` answer is a few kilobytes; a larger one is not the answer the check expects.
_MAX_TOKEN_CHECK_BYTES = 64 * 1024
_DEFAULT_ACCEPT: Final = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


class GitHubRequest(BaseModel):
    """One GitHub API call: `path` is `/graphql` or under `/repos/<owner>/<repo>`, with an optional query."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    body: JsonValue = None
    accept: Literal[
        "application/vnd.github+json",
        "application/vnd.github.full+json",
        "application/vnd.github.diff",
    ] = _DEFAULT_ACCEPT


class GitHubToken(BaseModel):
    """A personal access token: 1 to 512 visible ASCII characters, so no space, control, or other character."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512, pattern=r"^[!-~]+$")


def _path_kind(path: str) -> str | None:
    r"""Return how logs name ``path`` when a client may request it, else None.

    A segment that decodes to `.` or `..`, or to anything holding `/`, `\`, or a control character, is refused too,
    so no server on the way can resolve the path outside the repository.
    """
    match = _PATH.fullmatch(path) if len(path) <= _MAX_PATH_LENGTH else None
    if match is None:
        return None
    segments = [unquote(segment) for segment in match.group(1).split("/")[1:]]
    if any(s in {".", ".."} or "/" in s or "\\" in s or not s.isprintable() for s in segments):
        return None
    if segments[0] == "graphql":
        return "/graphql"
    if len(segments) == 3:
        return "/repos/{owner}/{repo}"
    resource = segments[3] if _RESOURCE.fullmatch(segments[3]) else "{other}"
    return f"/repos/{{owner}}/{{repo}}/{resource}"


def _github_client() -> httpx.AsyncClient:
    """Build the client for one request; it follows no redirect itself, since only `_redirect` may send the token on."""
    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False)


def _error(status_code: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status_code)


def _credential_context(request: Request) -> OAuthCredentialContext | None:
    """Return the session user's own GitHub connection, the one the `github` tool uses for them with any agent.

    None when the session resolves no person: such a context would name the unscoped store, which is no one's own.
    """
    user_id = presented_session(request).user_id
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    paths = snapshot.runtime_paths
    # A requester-scoped provider keeps one connection per person whatever the agent; the name only fills the identity.
    identity = build_requester_execution_identity(agent_name="oauth", runtime_paths=paths, requester_id=user_id)
    context = resolve_oauth_credential_context(
        github_oauth_provider(),
        paths,
        get_runtime_credentials_manager(paths),
        None,
        execution_identity=identity,
        config=snapshot.runtime_config,
    )
    return None if context.worker_target is None else context


async def _usable_credentials(context: OAuthCredentialContext | None) -> dict[str, Any] | None:
    """Return the stored connection while it can authenticate, without refreshing it or creating a store."""
    if context is None:
        return None
    credentials = (await load_oauth_credentials_status(context, create_store=False)).credentials
    return credentials if await _usable(context, credentials) else None


async def _usable(context: OAuthCredentialContext, credentials: dict[str, Any] | None) -> bool:
    """Check ``credentials`` against the GitHub App client, whose credential file is read in a thread."""
    return bool(credentials) and await asyncio.to_thread(
        oauth_credentials_usable,
        context.provider,
        context.runtime_paths,
        credentials,
    )


async def _access_token(context: OAuthCredentialContext | None) -> str | None:
    """Return a current access token, refreshing one about to expire, or None while the user is not connected."""
    if context is None or await _usable_credentials(context) is None:
        return None
    try:
        credentials = await refresh_oauth_credentials(context)
    except (OAuthCredentialUnreadableError, OAuthRefreshRejectedError):
        return None
    if not await _usable(context, credentials):
        return None
    token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
    return token if isinstance(token, str) and token else None


def _token_store(context: OAuthCredentialContext) -> CredentialsManager:
    """Return the user's primary-only store, the one their GitHub App connection is in, without any shared layer.

    `primary_built_tool=True` is what places the service there: a store no worker can read or write.
    """
    return credential_layers(
        _TOKEN_SERVICE,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
        primary_built_tool=True,
    ).writable_store()


def _read_token(context: OAuthCredentialContext) -> dict[str, str] | None:
    """Return the user's saved `{token, login}`, None when they saved none; blocks on the filesystem.

    Raises ``CredentialsUnreadableError`` for a saved document that cannot be read or holds no token and login.
    """
    document = _token_store(context).read_saved_credentials(_TOKEN_SERVICE)
    if document is None:
        return None
    token, login = document.get("token"), document.get("login")
    if isinstance(token, str) and token and isinstance(login, str):
        return {"token": token, "login": login}
    msg = f"Stored credentials for {_TOKEN_SERVICE} hold no token and login"
    raise CredentialsUnreadableError(msg)


async def _stored_token(context: OAuthCredentialContext | None) -> dict[str, str] | None:
    """Return the user's saved `{token, login}`, or None when they saved none or the session names no user.

    Only a missing token lets requests fall back to the GitHub App connection: an unreadable one raises
    ``CredentialsUnreadableError``, since losing access to a token is no request to act as another account.
    """
    if context is None:
        return None
    try:
        return await asyncio.to_thread(_read_token, context)
    except CredentialsUnreadableError:
        logger.warning("github_token_unreadable")
        raise


def _save_token(context: OAuthCredentialContext, document: dict[str, str]) -> None:
    """Save ``document`` as the user's token, replacing one that cannot be read; blocks on the filesystem."""
    store = _token_store(context)
    try:
        store.read_saved_credentials(_TOKEN_SERVICE)
    except CredentialsUnreadableError:
        # The store refuses to overwrite a document it cannot read, and the user's new token is meant to replace it.
        store.delete_credentials(_TOKEN_SERVICE)
    store.save_credentials(_TOKEN_SERVICE, document)


async def _status(context: OAuthCredentialContext | None) -> dict[str, object] | JSONResponse:
    """Answer which account requests go out with: the saved token, else the GitHub App connection, else none."""
    try:
        saved = await _stored_token(context)
    except CredentialsUnreadableError:
        return _error(503, _UNAVAILABLE)
    try:
        credentials = await _usable_credentials(context)
    except OAuthProviderError:
        if saved is None:
            return _error(503, _UNAVAILABLE)
        credentials = None
    if saved is not None:
        status: dict[str, object] = {"connected": True, "via": "token", "login": saved["login"]}
        return status | {"app_connected": True} if credentials is not None else status
    if credentials is None:
        return {"connected": False}
    login = oauth_verified_claim(credentials, "login")
    return {"connected": True, "via": "app"} | ({} if login is None else {"login": login})


async def _token_login(token: str) -> str | JSONResponse:
    """Return the login GitHub names for ``token``, or the error to answer when it refuses or fails to name one."""
    headers = {"Authorization": f"Bearer {token}", "Accept": _DEFAULT_ACCEPT, "X-GitHub-Api-Version": _API_VERSION}
    body = None
    try:
        async with (
            asyncio.timeout(_TOKEN_CHECK_TIMEOUT_SECONDS),
            _github_client() as client,
            client.stream("GET", f"{_GITHUB_API}/user", headers=headers) as response,
        ):
            # GitHub's status alone decides a failed check, so its body is never downloaded.
            if response.status_code == 200:
                content = await _bounded_body(response, _MAX_TOKEN_CHECK_BYTES)
                body = None if content is None else _answer_body(content, response.headers.get("content-type", ""))
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("github_token_check_failed", error_type=type(exc).__name__)
        return _error(502, "github_unreachable")
    if response.status_code in {401, 403}:
        logger.info("github_token_rejected", status=response.status_code)
        return _error(400, "github_token_rejected")
    login = body.get("login") if isinstance(body, dict) else None
    if not isinstance(login, str) or not login:
        logger.warning("github_token_check_failed", status=response.status_code)
        return _error(502, "github_unreachable")
    return login


@router.get("/status", response_model=None)
async def github_status(request: Request) -> dict[str, object] | JSONResponse:
    """Say which GitHub account the session's user has and its login, from stored state only."""
    return await _status(_credential_context(request))


@router.put("/token", response_model=None)
async def save_github_token(payload: GitHubToken, request: Request) -> dict[str, object] | JSONResponse:
    """Check the session user's personal access token with GitHub, then save it with its login as their own."""
    context = _credential_context(request)
    if context is None:
        return _error(409, "github_not_connected")
    login = await _token_login(payload.token)
    if isinstance(login, JSONResponse):
        return login
    await asyncio.to_thread(_save_token, context, {"token": payload.token, "login": login})
    logger.info("github_token_saved")
    return {"connected": True, "via": "token", "login": login}


@router.delete("/token", response_model=None)
async def delete_github_token(request: Request) -> dict[str, object] | JSONResponse:
    """Remove the session user's saved token, if any, and answer the status left, maybe the GitHub App connection."""
    context = _credential_context(request)
    if context is not None:
        await asyncio.to_thread(lambda: _token_store(context).delete_credentials(_TOKEN_SERVICE))
    return await _status(context)


@router.post("/request", response_model=None)
async def github_request(payload: GitHubRequest, request: Request) -> dict[str, object] | JSONResponse:
    """Send ``payload`` to GitHub as the session's user and answer with GitHub's status, body, and rate headers.

    Any GitHub answer is a 200 here; 409 means the user has no usable GitHub connection, so the client can fall back.
    A saved token goes out before the GitHub App connection, and GitHub refusing it is relayed, not acted on.
    """
    context = _credential_context(request)
    kind = _path_kind(payload.path)
    if kind is None:
        return _error(400, "github_path_not_allowed")
    try:
        saved = await _stored_token(context)
        token = saved["token"] if saved is not None else await _access_token(context)
    except (CredentialsUnreadableError, OAuthProviderError):
        return _error(503, _UNAVAILABLE)
    if token is None:
        return _error(409, "github_not_connected")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": payload.accept,
        "X-GitHub-Api-Version": _API_VERSION,
    }
    method, url = payload.method, httpx.URL(_GITHUB_API + payload.path)
    content = None if payload.body is None else json.dumps(payload.body).encode()
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS), _github_client() as client:
            for redirects in range(_MAX_REDIRECTS + 1):
                typed = {"Content-Type": "application/json"} if content is not None else {}
                async with client.stream(method, url, headers=headers | typed, content=content) as response:
                    hop = _redirect(response, method, content) if redirects < _MAX_REDIRECTS else None
                    if hop is None:
                        body = await _bounded_body(response, _MAX_RESPONSE_BYTES)
                        if body is None:
                            logger.warning("github_request_too_large", method=payload.method, path=kind)
                            return _error(502, "github_response_too_large")
                        break
                method, url, content = hop
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("github_request_failed", method=payload.method, path=kind, error_type=type(exc).__name__)
        return _error(502, "github_unreachable")
    logger.info("github_request", method=payload.method, path=kind, status=response.status_code, redirects=redirects)
    return {
        "status": response.status_code,
        "body": _answer_body(body, response.headers.get("content-type", "")),
        "headers": {name: response.headers[name] for name in _PASSED_HEADERS if name in response.headers},
    }


def _redirect(
    response: httpx.Response,
    method: str,
    content: bytes | None,
) -> tuple[str, httpx.URL, bytes | None] | None:
    """Return the method, URL, and body a browser would send next for a redirect within `https://api.github.com`.

    None for any other answer, and for a 301 or 302 to a write, so a write is never sent again by surprise.
    """
    url = None if response.next_request is None else response.next_request.url
    if url is None or (url.scheme, url.host, url.port, url.userinfo) != ("https", "api.github.com", None, b""):
        return None
    if response.status_code == 303:
        return "GET", url, None
    if response.status_code in {307, 308} or method == "GET":
        return method, url, content
    return None


async def _bounded_body(response: httpx.Response, limit: int) -> bytes | None:
    """Return the streamed ``response`` body, or None as soon as it grows past ``limit`` bytes."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _answer_body(body: bytes, content_type: str) -> JsonValue:
    """Return GitHub's JSON when its media type says JSON, else its text, or None when it sent nothing.

    An answer holding a number that is not finite, such as `NaN`, which no JSON answer can carry, is text too.
    """
    if not body:
        return None
    text = body.decode(errors="replace")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return text
    try:
        return json.loads(text, parse_constant=_not_finite, parse_float=_finite_float)
    except ValueError:
        return text


def _not_finite(number: str) -> NoReturn:
    msg = f"{number} is not a finite number"
    raise ValueError(msg)


def _finite_float(number: str) -> float:
    value = float(number)
    return value if math.isfinite(value) else _not_finite(number)

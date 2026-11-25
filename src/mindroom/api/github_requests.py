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
import weakref
from typing import TYPE_CHECKING, Any, Literal, NoReturn, override
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mindroom.api import config_lifecycle
from mindroom.api.client_sessions import presented_session
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.credentials.manager import CredentialsUnreadableError, credential_layers
from mindroom.github_client import (
    GITHUB_API_BASE,
    GITHUB_API_VERSION,
    GITHUB_DEFAULT_ACCEPT,
    GITHUB_LOGIN_SHAPE,
    GITHUB_TOKEN_SHAPE,
    GITHUB_USER_TOKEN_SERVICE,
    build_user_github_oauth_context,
    github_redirect_next,
    github_user_token_store,
    is_github_rate_limited,
    load_usable_github_oauth_credentials,
    matches_github_shape,
    read_bounded_body,
    read_saved_github_token,
    refresh_github_oauth_access_token,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    load_oauth_credentials_status,
    oauth_credentials_usable,
    oauth_verified_claim,
)
from mindroom.oauth.providers import OAuthProviderError

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

_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# GitHub redirects a renamed or transferred repository's old path to `/repositories/<id>/...` on the same host.
_MAX_REDIRECTS = 3
# Far longer than any GitHub API path a client builds, and far below the longest URL httpx sends.
_MAX_PATH_LENGTH = 4096
# The stored connection could not be read or refreshed for a reason a retry may fix.
_UNAVAILABLE = "github_connection_unavailable"
# The saved token could not be read; only saving another or removing it helps, so a retry does not.
_TOKEN_UNREADABLE = "github_token_unreadable"  # noqa: S105
_PASSED_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-reset", "retry-after", "x-github-request-id")
# One RFC 3986 path character, a `%` always followed by two hex digits. This leaves out controls, spaces, `\`, `#`,
# and anything outside ASCII, and since every segment is non-empty, `//`, so no path can name another host.
_PCHAR = r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})"
# Logs name the segment after the repository only when it reads as an API resource, such as `pulls` or `check-runs`.
_RESOURCE = re.compile(r"[a-z][a-z_-]{0,31}")
_PATH = re.compile(rf"(/graphql|/repos(?:/{_PCHAR}+){{2,}})(?:\?(?:{_PCHAR}|[/?])*)?")
_TOKEN_CHECK_TIMEOUT_SECONDS = 10.0
# GitHub's `/user` answer is a few kilobytes; a larger one is not the answer the check expects.
_MAX_TOKEN_CHECK_BYTES = 64 * 1024
# One lock per user with a token save or removal under way, so a removal sent during a save's GitHub check runs after
# that save instead of being undone by it. Per process only: processes sharing one credential store do not share it.
# A lock no request holds or waits for drops out.
_token_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


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
        "application/vnd.github.raw+json",
    ] = GITHUB_DEFAULT_ACCEPT
    max_bytes: int = Field(default=_MAX_RESPONSE_BYTES, ge=1, le=_MAX_RESPONSE_BYTES, strict=True)


class GitHubToken(BaseModel):
    """A personal access token: 1 to 512 visible ASCII characters, so no space, control, or other character."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(pattern=rf"^{GITHUB_TOKEN_SHAPE.pattern}$")


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
    """Build the client for one request; it follows no redirect itself, since only `github_redirect_next` may."""
    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False)


def _error(status_code: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status_code)


def _credential_context(request: Request) -> OAuthCredentialContext | None:
    """Return the session user's own GitHub connection, the one the `github` tool uses for them with any agent."""
    user_id = presented_session(request).user_id
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    return build_user_github_oauth_context(user_id, snapshot.runtime_config, snapshot.runtime_paths)


async def _usable_credentials(context: OAuthCredentialContext | None) -> dict[str, Any] | None:
    """Return the stored connection while it can authenticate, without refreshing it or creating a store."""
    return await load_usable_github_oauth_credentials(
        context,
        load_status=load_oauth_credentials_status,
        is_usable=oauth_credentials_usable,
    )


async def _access_token(context: OAuthCredentialContext | None) -> str | None:
    """Return a current access token, refreshing one about to expire, or None while the user is not connected."""
    return await refresh_github_oauth_access_token(
        context,
        load_status=load_oauth_credentials_status,
        is_usable=oauth_credentials_usable,
    )


def _token_store(context: OAuthCredentialContext) -> CredentialsManager:
    """Return the user's primary-only store, the one their GitHub App connection is in, without any shared layer."""
    return github_user_token_store(context, get_layers=credential_layers)


async def _stored_token(context: OAuthCredentialContext | None) -> dict[str, str] | None:
    """Return the user's saved `{token, login}`, or None when they saved none or the session names no user.

    Only a missing token lets requests fall back to the GitHub App connection: an unreadable one raises
    ``CredentialsUnreadableError``, since losing access to a token is no request to act as another account.
    """
    if context is None:
        return None
    try:
        return await asyncio.to_thread(read_saved_github_token, context, get_layers=credential_layers)
    except CredentialsUnreadableError:
        logger.warning("github_token_unreadable")
        raise


def _token_lock(user_id: str) -> asyncio.Lock:
    """Return the lock serializing ``user_id``'s token saves and removals in this process."""
    lock = _token_locks.get(user_id)
    if lock is None:
        lock = _token_locks[user_id] = asyncio.Lock()
    return lock


def _save_token(context: OAuthCredentialContext, document: dict[str, str]) -> None:
    """Save ``document`` as the user's token, replacing even one that cannot be read; blocks on the filesystem."""
    _token_store(context).save_credentials(GITHUB_USER_TOKEN_SERVICE, document, replace_unreadable=True)


async def _status(context: OAuthCredentialContext | None) -> dict[str, object] | JSONResponse:
    """Answer which account requests go out with: the saved token, else the GitHub App connection, else none."""
    try:
        saved = await _stored_token(context)
    except CredentialsUnreadableError:
        return _error(503, _TOKEN_UNREADABLE)
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
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": GITHUB_DEFAULT_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    body = None
    try:
        async with (
            asyncio.timeout(_TOKEN_CHECK_TIMEOUT_SECONDS),
            _github_client() as client,
            client.stream("GET", f"{GITHUB_API_BASE}/user", headers=headers) as response,
        ):
            # GitHub's status alone decides a failed check, so its body is never downloaded.
            if response.status_code == 200:
                content = await read_bounded_body(response, _MAX_TOKEN_CHECK_BYTES)
                body = None if content is None else _answer_body(content, response.headers.get("content-type", ""))
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("github_token_check_failed", error_type=type(exc).__name__)
        return _error(502, "github_unreachable")
    if is_github_rate_limited(response):
        logger.warning("github_token_check_rate_limited", status=response.status_code)
        return _error(502, "github_rate_limited")
    if response.status_code in {401, 403}:
        logger.info("github_token_rejected", status=response.status_code)
        return _error(400, "github_token_rejected")
    login = body.get("login") if isinstance(body, dict) else None
    if not matches_github_shape(login, GITHUB_LOGIN_SHAPE):
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
    async with _token_lock(presented_session(request).user_id):
        login = await _token_login(payload.token)
        if isinstance(login, JSONResponse):
            return login
        try:
            # Finished even when the request is cancelled, so the lock is never released while it still writes.
            await run_blocking_until_complete(_save_token, context, {"token": payload.token, "login": login})
        except (OSError, ValueError) as exc:
            # The store refuses some writes with `ValueError`; neither answer names the path or the error.
            logger.warning("github_token_save_failed", error_type=type(exc).__name__)
            return _error(503, _UNAVAILABLE)
    logger.info("github_token_saved")
    return {"connected": True, "via": "token", "login": login}


@router.delete("/token", response_model=None)
async def delete_github_token(request: Request) -> dict[str, object] | JSONResponse:
    """Remove the session user's saved token, if any, and answer the status left, maybe the GitHub App connection."""
    context = _credential_context(request)
    if context is not None:
        # Waits for a save under way, which would otherwise publish its token after this removal.
        async with _token_lock(presented_session(request).user_id):
            try:
                await run_blocking_until_complete(
                    lambda: _token_store(context).delete_credentials(GITHUB_USER_TOKEN_SERVICE),
                )
            except (OSError, ValueError) as exc:
                # As on save, the store may refuse with `ValueError`; the answer names neither path nor error.
                logger.warning("github_token_delete_failed", error_type=type(exc).__name__)
                return _error(503, _UNAVAILABLE)
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
    except (CredentialsUnreadableError, OAuthProviderError) as exc:
        return _error(503, _TOKEN_UNREADABLE if isinstance(exc, CredentialsUnreadableError) else _UNAVAILABLE)
    if token is None:
        return _error(409, "github_not_connected")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": payload.accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    method: str = payload.method
    url = httpx.URL(GITHUB_API_BASE + payload.path)
    content = None if payload.body is None else json.dumps(payload.body).encode()
    body: bytes | None = None
    response: httpx.Response | None = None
    redirects = 0
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS), _github_client() as client:
            for redirects in range(_MAX_REDIRECTS + 1):
                typed = {"Content-Type": "application/json"} if content is not None else {}
                async with client.stream(method, url, headers=headers | typed, content=content) as response:
                    hop = github_redirect_next(response, method, content) if redirects < _MAX_REDIRECTS else None
                    if hop is None:
                        body = await read_bounded_body(response, payload.max_bytes)
                        break
                method, url, content = hop
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("github_request_failed", method=payload.method, path=kind, error_type=type(exc).__name__)
        return _error(502, "github_unreachable")
    if response is None or body is None:
        logger.warning("github_request_too_large", method=payload.method, path=kind)
        return _error(502, "github_response_too_large")
    logger.info("github_request", method=payload.method, path=kind, status=response.status_code, redirects=redirects)
    return _request_answer(response, body, accept=payload.accept, method=payload.method, path=kind)


def _request_answer(
    response: httpx.Response,
    body: bytes,
    *,
    accept: str,
    method: str,
    path: str,
) -> dict[str, object] | JSONResponse:
    """Relay the upstream response, requiring strict UTF-8 for successful raw answers."""
    try:
        answer = (
            body.decode("utf-8")
            if response.is_success and accept == "application/vnd.github.raw+json"
            else _answer_body(body, response.headers.get("content-type", ""))
        )
    except UnicodeDecodeError:
        logger.warning("github_request_not_text", method=method, path=path)
        return _error(502, "github_response_not_text")
    return {
        "status": response.status_code,
        "body": answer,
        "headers": {name: response.headers[name] for name in _PASSED_HEADERS if name in response.headers},
    }


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

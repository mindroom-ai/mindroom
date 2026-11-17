"""`/api/github`: GitHub API requests a signed-in client makes with its user's own connected GitHub account.

The client names a method and a repository or GraphQL path; this route adds the user's token, sends the request to
`api.github.com` only, and answers with GitHub's status, body, and a few headers, so the token never leaves MindRoom.
Logs carry the method, the path's kind, and GitHub's status, never the token, the repository, or a body.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, JsonValue

from mindroom.api import config_lifecycle
from mindroom.api.client_sessions import presented_session
from mindroom.credentials.manager import get_runtime_credentials_manager
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
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext

logger = get_logger(__name__)

router = APIRouter(prefix="/api/github")

_GITHUB_API = "https://api.github.com"
_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# Far longer than any GitHub API path a client builds, and far below the longest URL httpx sends.
_MAX_PATH_LENGTH = 4096
_DIFF = "application/vnd.github.diff"
_PASSED_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-reset", "retry-after", "x-github-request-id")
# One RFC 3986 path character, a `%` always followed by two hex digits. This leaves out controls, spaces, `\`, `#`,
# and anything outside ASCII, and since every segment is non-empty, `//`, so no path can name another host.
_PCHAR = r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})"
_PATH = re.compile(rf"(/graphql|/repos(?:/{_PCHAR}+){{2,}})(?:\?(?:{_PCHAR}|[/?])*)?")


def _scrub_github_url(record: logging.LogRecord) -> bool:
    """Cut the path and query from httpx's line for a request to GitHub's API, which would name a repository."""
    if isinstance(record.args, tuple):
        record.args = tuple(
            f"{_GITHUB_API}/…" if isinstance(arg, httpx.URL) and arg.host == "api.github.com" else arg
            for arg in record.args
        )
    return True


# httpx logs each request's full URL at INFO; a logger's filters outlast every later `dictConfig`.
logging.getLogger("httpx").addFilter(_scrub_github_url)


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
    ] = "application/vnd.github+json"


def _path_kind(path: str) -> str | None:
    r"""Return how logs name ``path`` when a client may request it, else None.

    A segment that decodes to `.` or `..`, or to anything holding `/`, `\\`, or a control character, is refused too,
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
    return "/repos/{owner}/{repo}" + "".join(f"/{s}" for s in segments[3:4])


def _github_client() -> httpx.AsyncClient:
    """Build the client for one request; it never follows a redirect, so the token goes to `api.github.com` only."""
    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False)


def _error(status_code: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status_code)


def _credential_context(request: Request) -> OAuthCredentialContext:
    """Return the session user's own GitHub connection, the one the `github` tool uses for them with any agent."""
    user_id = presented_session(request).user_id
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    paths = snapshot.runtime_paths
    # A requester-scoped provider keeps one connection per person whatever the agent; the name only fills the identity.
    identity = build_requester_execution_identity(agent_name="oauth", runtime_paths=paths, requester_id=user_id)
    return resolve_oauth_credential_context(
        github_oauth_provider(),
        paths,
        get_runtime_credentials_manager(paths),
        None,
        execution_identity=identity,
        config=snapshot.runtime_config,
    )


async def _usable_credentials(context: OAuthCredentialContext) -> dict[str, Any] | None:
    """Return the stored connection while it can authenticate, without refreshing it or creating a store."""
    credentials = (await load_oauth_credentials_status(context, create_store=False)).credentials
    return credentials if oauth_credentials_usable(context.provider, context.runtime_paths, credentials) else None


async def _access_token(context: OAuthCredentialContext) -> str | None:
    """Return a current access token, refreshing one about to expire, or None while the user is not connected."""
    if await _usable_credentials(context) is None:
        return None
    try:
        credentials = await refresh_oauth_credentials(context)
    except (OAuthCredentialUnreadableError, OAuthRefreshRejectedError):
        return None
    if not oauth_credentials_usable(context.provider, context.runtime_paths, credentials):
        return None
    token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
    return token if isinstance(token, str) and token else None


@router.get("/status")
async def github_status(request: Request) -> dict[str, object]:
    """Say whether the session's user has a usable GitHub connection and its login, from stored state only."""
    credentials = await _usable_credentials(_credential_context(request))
    if credentials is None:
        return {"connected": False}
    login = oauth_verified_claim(credentials, "login")
    return {"connected": True} if login is None else {"connected": True, "login": login}


@router.post("/request", response_model=None)
async def github_request(payload: GitHubRequest, request: Request) -> dict[str, object] | JSONResponse:
    """Send ``payload`` to GitHub as the session's user and answer with GitHub's status, body, and rate headers.

    Any GitHub answer is a 200 here; 409 means the user has no usable GitHub connection, so the client can fall back.
    """
    context = _credential_context(request)
    kind = _path_kind(payload.path)
    if kind is None:
        return _error(400, "github_path_not_allowed")
    try:
        token = await _access_token(context)
    except OAuthProviderError:
        return _error(503, "github_refresh_failed")
    if token is None:
        return _error(409, "github_not_connected")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": payload.accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    content = None if payload.body is None else json.dumps(payload.body).encode()
    if content is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with (
            _github_client() as client,
            client.stream(payload.method, _GITHUB_API + payload.path, headers=headers, content=content) as response,
        ):
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > _MAX_RESPONSE_BYTES:
                    logger.warning("github_request_too_large", method=payload.method, path=kind)
                    return _error(502, "github_response_too_large")
    except httpx.HTTPError as exc:
        logger.warning("github_request_failed", method=payload.method, path=kind, error_type=type(exc).__name__)
        return _error(502, "github_unreachable")
    logger.info("github_request", method=payload.method, path=kind, status=response.status_code)
    return {
        "status": response.status_code,
        "body": _answer_body(bytes(body), diff=payload.accept == _DIFF),
        "headers": {name: response.headers[name] for name in _PASSED_HEADERS if name in response.headers},
    }


def _answer_body(body: bytes, *, diff: bool) -> JsonValue:
    """Return GitHub's JSON, or its text for a diff or an answer that is not JSON, or None when it sent nothing."""
    if not body:
        return None
    text = body.decode(errors="replace")
    if diff:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text

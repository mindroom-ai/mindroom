"""Shared owner-scoped GitHub credentials and fixed-host `https://api.github.com` HTTP helpers."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from contextlib import AsyncExitStack, suppress
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, TypeIs

import httpx

from mindroom.credentials.manager import CredentialsUnreadableError, credential_layers, get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.oauth import credential_lifecycle as oauth_lc
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthRefreshRejectedError
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialLayers, CredentialsManager

logger = get_logger(__name__)
GITHUB_API_BASE: Final = "https://api.github.com"
GITHUB_API_VERSION: Final = "2022-11-28"
GITHUB_DEFAULT_ACCEPT: Final = "application/vnd.github+json"
GITHUB_USER_TOKEN_SERVICE: Final = "github_user_token"  # noqa: S105
GITHUB_TOKEN_SHAPE, GITHUB_LOGIN_SHAPE = re.compile(r"[!-~]{1,512}"), re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
GitHubAuthMode = Literal["token", "app", "anonymous"]
_GITHUB_ORIGIN: Final = ("https", "api.github.com", None, b"")
_MAX_REDIRECTS, _MAX_ERROR_BODY_BYTES = 3, 64 * 1024
_LINK_ENTRY_RE = re.compile(r"<([^>\s]+)>((?:\s*;\s*[^,;]+)*)")
_LINK_REL_RE = re.compile(r';\s*rel\s*=\s*(?:"([^"]*)"|([^\s;]+))', re.IGNORECASE)
type _Ctx = oauth_lc.OAuthCredentialContext
type _Hop = tuple[str, httpx.URL, bytes | None]
type _Tok = tuple[str | None, GitHubAuthMode]
type _LayersFn = Callable[..., CredentialLayers]


class GitHubClientError(RuntimeError):
    """Raised when a GitHub API response exceeds bounds or cannot be decoded as expected JSON."""


class GitHubGetResult(NamedTuple):
    """Result of one fixed-host `https://api.github.com` GET request."""

    status_code: int
    etag: str | None
    data: Any
    headers: dict[str, str]
    rate_limited: bool
    retry_after_seconds: float | None
    next_page: int | None = None
    last_page: int | None = None


def matches_github_shape(value: object, shape: re.Pattern[str]) -> TypeIs[str]:
    """Return whether ``value`` is a string matching ``shape`` completely."""
    return isinstance(value, str) and shape.fullmatch(value) is not None


def build_user_github_oauth_context(user_id: str, config: Config | None, runtime_paths: RuntimePaths) -> _Ctx | None:
    """Return ``user_id``'s own GitHub OAuth credential context, or None when no requester scope resolves."""
    ident = build_requester_execution_identity(agent_name="oauth", runtime_paths=runtime_paths, requester_id=user_id)
    mgr, resolve = get_runtime_credentials_manager(runtime_paths), oauth_lc.resolve_oauth_credential_context
    ctx = resolve(github_oauth_provider(), runtime_paths, mgr, None, execution_identity=ident, config=config)
    return None if ctx.worker_target is None else ctx


def github_user_token_store(context: _Ctx, *, get_layers: _LayersFn = credential_layers) -> CredentialsManager:
    """Return the user's primary-only store for their saved GitHub personal access token."""
    kw = {"credentials_manager": context.credentials_manager, "worker_target": context.worker_target}
    return get_layers(GITHUB_USER_TOKEN_SERVICE, **kw, primary_built_tool=True).writable_store()


def read_saved_github_token(context: _Ctx, *, get_layers: _LayersFn = credential_layers) -> dict[str, str] | None:
    """Return the user's saved ``{token, login}`` or None; raises ``CredentialsUnreadableError`` if unreadable."""
    try:
        doc = github_user_token_store(context, get_layers=get_layers).read_saved_credentials(GITHUB_USER_TOKEN_SERVICE)
    except ValueError as exc:
        msg = f"Stored credentials for {GITHUB_USER_TOKEN_SERVICE} could not be located"
        raise CredentialsUnreadableError(msg) from exc
    if doc is None:
        return None
    tok, login = doc.get("token"), doc.get("login")
    if matches_github_shape(tok, GITHUB_TOKEN_SHAPE) and matches_github_shape(login, GITHUB_LOGIN_SHAPE):
        return {"token": tok, "login": login}
    msg = f"Stored credentials for {GITHUB_USER_TOKEN_SERVICE} hold no token and login"
    raise CredentialsUnreadableError(msg)


async def load_usable_github_oauth_credentials(
    context: _Ctx | None,
    *,
    load_status: Callable[..., Awaitable[oauth_lc.OAuthCredentialsStatus]] = oauth_lc.load_oauth_credentials_status,
    is_usable: Callable[..., bool] = oauth_lc.oauth_credentials_usable,
) -> dict[str, Any] | None:
    """Return stored GitHub OAuth credentials while usable, without refreshing or creating a store."""
    if context is None or not (creds := (await load_status(context, create_store=False)).credentials):
        return None
    return creds if await asyncio.to_thread(is_usable, context.provider, context.runtime_paths, creds) else None


async def refresh_github_oauth_access_token(
    context: _Ctx | None,
    *,
    load_status: Callable[..., Awaitable[oauth_lc.OAuthCredentialsStatus]] = oauth_lc.load_oauth_credentials_status,
    is_usable: Callable[..., bool] = oauth_lc.oauth_credentials_usable,
) -> str | None:
    """Return a current GitHub OAuth access token, refreshing if needed, or None when unconnected."""
    if not await load_usable_github_oauth_credentials(context, load_status=load_status, is_usable=is_usable):
        return None
    assert context is not None
    with suppress(oauth_lc.OAuthCredentialUnreadableError, OAuthRefreshRejectedError):
        creds = await oauth_lc.refresh_oauth_credentials(context)
        if creds is not None and await asyncio.to_thread(is_usable, context.provider, context.runtime_paths, creds):
            tok = creds.get("token") or creds.get("access_token")
            return tok if isinstance(tok, str) and tok else None
    return None


async def resolve_owner_github_token(owner: str, config: Config | None, paths: RuntimePaths) -> _Tok:
    """Resolve one owner's GitHub token: saved PAT first, else GitHub App OAuth, else anonymous."""
    if (ctx := build_user_github_oauth_context(owner, config, paths)) is None:
        return None, "anonymous"
    try:
        if (saved := await asyncio.to_thread(read_saved_github_token, ctx)) is not None:
            return saved["token"], "token"
    except CredentialsUnreadableError:
        logger.warning("github_token_unreadable")
        raise
    return (tok, "app") if (tok := await refresh_github_oauth_access_token(ctx)) is not None else (None, "anonymous")


def github_redirect_next(response: httpx.Response, method: str, content: bytes | None) -> _Hop | None:
    """Return the next ``(method, url, content)`` for a redirect strictly within ``https://api.github.com``."""
    url = None if response.next_request is None else response.next_request.url
    if url is None or (url.scheme, url.host, url.port, url.userinfo) != _GITHUB_ORIGIN:
        return None
    if response.status_code == 303:
        return "GET", url, None
    return (method, url, content) if (response.status_code in {307, 308} or method == "GET") else None


async def read_bounded_body(response: httpx.Response, limit: int) -> bytes | None:
    """Return the streamed ``response`` body, or None as soon as it grows past ``limit`` bytes."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def is_github_rate_limited(response: httpx.Response) -> bool:
    """Return whether GitHub answered with a rate limit: any 429, or a 403 with 0 remaining or Retry-After."""
    limited = response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
    return response.status_code == 429 or (response.status_code == 403 and limited)


def parse_retry_after_seconds(headers: Mapping[str, str], *, now: float | None = None) -> float | None:
    """Return positive wait seconds from ``Retry-After`` or ``X-RateLimit-Reset`` (when remaining is 0)."""
    norm, cur = {k.lower(): v.strip() for k, v in headers.items()}, time.time() if now is None else now
    if raw := norm.get("retry-after", ""):
        with suppress(ValueError):
            if math.isfinite(val := float(raw)) and val > 0:
                return val
        with suppress(TypeError, ValueError, OverflowError):
            dt = parsedate_to_datetime(raw)
            wait = (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).timestamp() - cur
            return wait if math.isfinite(wait) and wait > 0 else None
    if norm.get("x-ratelimit-remaining") == "0" and (reset := norm.get("x-ratelimit-reset", "")):
        with suppress(ValueError):
            return val if math.isfinite(val := float(reset) - cur) and val > 0 else None
    return None


def _parse_link_pages(link_header: str | None) -> tuple[int | None, int | None]:
    nxt = last = None
    for raw_url, params in _LINK_ENTRY_RE.findall(link_header or ""):
        rels = (m.group(1) or m.group(2) or "").lower().split() if (m := _LINK_REL_RE.search(params)) else []
        if not ({"next", "last"} & set(rels)):
            continue
        with suppress(httpx.InvalidURL):
            url = httpx.URL(raw_url)
            raw_p = url.params.get("page") if (url.scheme, url.host, url.port, url.userinfo) == _GITHUB_ORIGIN else None
            if raw_p is not None and raw_p.isascii() and raw_p.isdigit() and (page := int(raw_p)) >= 1:
                nxt = page if "next" in rels and nxt is None else nxt
                last = page if "last" in rels and last is None else last
    return nxt, last


async def _build_get_result(response: httpx.Response, request_etag: str | None, max_bytes: int) -> GitHubGetResult:
    headers = {name.lower(): value for name, value in response.headers.items()}
    if response.status_code == 304:
        return GitHubGetResult(304, headers.get("etag") or request_etag, None, headers, False, None)
    if (body := await read_bounded_body(response, max_bytes)) is None:
        msg = f"GitHub API response exceeded {max_bytes} bytes"
        raise GitHubClientError(msg)
    limited, retry, etag = is_github_rate_limited(response), parse_retry_after_seconds(headers), headers.get("etag")
    if response.status_code == 200:
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = "GitHub API returned invalid JSON"
            raise GitHubClientError(msg) from exc
        return GitHubGetResult(200, etag, data, headers, limited, retry, *_parse_link_pages(headers.get("link")))
    err: dict[str, Any] | None = None
    with suppress(UnicodeDecodeError, json.JSONDecodeError):
        if body and len(body) <= _MAX_ERROR_BODY_BYTES and isinstance(dec := json.loads(body), dict):
            err = dec
    return GitHubGetResult(response.status_code, etag, err, headers, limited, retry)


async def github_get_json(
    path: str,
    *,
    token: str | None,
    etag: str | None = None,
    timeout_seconds: float = 20.0,
    max_bytes: int = 2 * 1024 * 1024,
    client: httpx.AsyncClient | None = None,
) -> GitHubGetResult:
    """Send one safe, bounded GET request to ``https://api.github.com`` and decode JSON on 200."""
    bad = not path.startswith("/") or path.startswith("//") or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
    url = None if bad else httpx.URL(GITHUB_API_BASE + path)
    if url is None or (url.scheme, url.host, url.port, url.userinfo) != _GITHUB_ORIGIN:
        msg = f"GitHub API path must target {GITHUB_API_BASE}: {path!r}"
        raise ValueError(msg)
    hdrs = {"Accept": GITHUB_DEFAULT_ACCEPT, "X-GitHub-Api-Version": GITHUB_API_VERSION, "User-Agent": "mindroom"}
    hdrs |= ({"Authorization": f"Bearer {token}"} if token else {}) | ({"If-None-Match": etag} if etag else {})
    method = "GET"
    async with asyncio.timeout(timeout_seconds), AsyncExitStack() as stack:
        cm = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        http = client if client is not None else await stack.enter_async_context(cm)
        for hops in range(_MAX_REDIRECTS + 1):
            async with http.stream(method, url, headers=hdrs, follow_redirects=False) as resp:
                if (hop := github_redirect_next(resp, method, None) if hops < _MAX_REDIRECTS else None) is None:
                    return await _build_get_result(resp, etag, max_bytes)
                method, url, _ = hop
    msg = "GitHub API redirect loop terminated unexpectedly"
    raise GitHubClientError(msg)

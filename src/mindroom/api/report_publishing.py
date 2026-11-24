"""Published report API routes."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from mindroom.api.auth import require_connections_user
from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.api.report_headers import set_report_headers
from mindroom.origins import connections_sign_in_page, deployment_origin, published_reports_require_sign_in
from mindroom.report_publishing.store import PUBLIC_REPORT_SLUG_RE, ReportPublishingError, ReportPublishingStore

public_router = APIRouter(tags=["report-publishing"])

# A link is the only key to a report in the open mode, so it must not travel in a referrer or reach a search index.
_PUBLISHED_REPORT_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}
_NO_STORE_HEADERS = {**_PUBLISHED_REPORT_HEADERS, "Cache-Control": "no-store"}
_SIGN_IN_REQUIRED_PAGE = (
    "<!doctype html><title>Sign-in required</title><p>Sign-in required. "
    "Open Connections from MindRoom Chat to sign in, then open this link again.</p>"
)
# A sandboxed static site has an opaque origin, so its assets carry no cookie: after the sign-in check, the site is
# served under a view link segment, `~<expiry>.<signature>`, that opens this one slug until the expiry.
_VIEW_LINK_SECONDS = 4 * 3600
# A new key per process, as for portal sessions, so a restart ends every view link.
_VIEW_LINK_KEY = secrets.token_bytes(32)
_VIEW_LINK_SEGMENT = re.compile(r"~(\d{1,12})\.([0-9a-f]{64})")
# Remembers, across Chat's sign-in, the report page and query that sent the browser there.
_RETURN_COOKIE = "mindroom_report_return"
_RETURN_SECONDS = 600


@public_router.get("/reports/public/{slug}", include_in_schema=False)
async def public_report(request: Request, slug: str) -> Response:
    """Serve one active published HTML report from runtime storage."""
    return await _published_report_response(request, slug, None, redirect_static_site_to_slash=True)


@public_router.get("/reports/public/{slug}/", include_in_schema=False)
async def public_report_index(request: Request, slug: str) -> Response:
    """Serve one active published static-site index from runtime storage."""
    return await _published_report_response(request, slug, None)


@public_router.get("/reports/public/{slug}/{asset_path:path}", include_in_schema=False)
async def public_report_asset(request: Request, slug: str, asset_path: str) -> Response:
    """Serve one active published static-site asset from runtime storage."""
    return await _published_report_response(request, slug, asset_path)


async def _published_report_response(
    request: Request,
    slug: str,
    asset_path: str | None,
    *,
    redirect_static_site_to_slash: bool = False,
) -> Response:
    if not published_reports_require_sign_in(api_runtime_paths(request)):
        return _public_report_asset_response(
            request,
            slug,
            asset_path,
            redirect_static_site_to_slash=redirect_static_site_to_slash,
        )
    # Nothing below reads the store before the view link or the session checks out, so a signed-out request cannot
    # tell a live slug from a revoked or unknown one.
    segment, slash, rest = (asset_path or "").partition("/")
    view_link = _VIEW_LINK_SEGMENT.fullmatch(segment)
    if view_link is not None and slash and _view_link_valid(slug, view_link):
        response = _public_report_asset_response(request, slug, rest or None)
        # Module scripts and CSS fonts are CORS requests from the site's opaque origin; the view link is the credential.
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    # A stale view link is replaced, keeping the asset it led to.
    asset = rest if view_link is not None else asset_path
    # `request.url` is rebuilt from the decoded path, so a `#` or `?` in a file name would swallow its query.
    query_string = request.scope["query_string"].decode("latin-1")
    query = f"?{query_string}" if query_string else ""
    try:
        await require_connections_user(request)
    except HTTPException as error:
        if error.status_code not in {401, 403}:
            raise
        return _sign_in_response(request, slug, asset, query)
    if asset and _leaves_report(asset):
        # A browser would resolve the view link's dot segments to a path outside the report.
        raise HTTPException(status_code=404, detail="Public report was not found.")
    return RedirectResponse(
        f"/reports/public/{quote(slug, safe='')}/{_view_link(slug)}/{quote(asset or '')}{query}",
        status_code=303,
        headers=_NO_STORE_HEADERS,
    )


def _public_report_asset_response(
    request: Request,
    slug: str,
    asset_path: str | None,
    *,
    redirect_static_site_to_slash: bool = False,
) -> Response:
    runtime_paths = api_runtime_paths(request)
    store = ReportPublishingStore(runtime_paths.storage_root)
    try:
        report = store.get_public_report(slug)
        if report.is_static_site and redirect_static_site_to_slash:
            # Relative-URL assets only resolve under the trailing-slash form,
            # and a relative Location keeps any subpath proxy prefix intact.
            return RedirectResponse(url=f"{slug}/", status_code=301)
        report_path = store.report_asset_path(report, asset_path)
    except ReportPublishingError as exc:
        raise HTTPException(status_code=404, detail="Public report was not found.") from exc

    response = FileResponse(report_path, headers=_PUBLISHED_REPORT_HEADERS)
    set_report_headers(
        response,
        cache_control="no-store, max-age=0",
        sandboxed_static_site=report.is_static_site,
    )
    return response


def _view_link_signature(slug: str, expires_at: str) -> str:
    return hmac.new(_VIEW_LINK_KEY, f"{slug}|{expires_at}".encode(), hashlib.sha256).hexdigest()


def _view_link(slug: str) -> str:
    expires_at = str(int(time.time()) + _VIEW_LINK_SECONDS)
    return f"~{expires_at}.{_view_link_signature(slug, expires_at)}"


def _view_link_valid(slug: str, view_link: re.Match[str]) -> bool:
    expires_at, signature = view_link.groups()
    return time.time() < int(expires_at) and hmac.compare_digest(signature, _view_link_signature(slug, expires_at))


def _sign_in_response(request: Request, slug: str, asset: str | None, query: str) -> Response:
    """Send a signed-out browser to Chat's sign-in page, remembering the report page and query to come back to."""
    sign_in_url = connections_sign_in_page(api_runtime_paths(request))
    if sign_in_url is None:
        return HTMLResponse(_SIGN_IN_REQUIRED_PAGE, status_code=401, headers=_NO_STORE_HEADERS)
    response = RedirectResponse(sign_in_url, status_code=303, headers=_NO_STORE_HEADERS)
    if PUBLIC_REPORT_SLUG_RE.fullmatch(slug):
        name, secure = _return_cookie(request)
        # Chat's hand-off is a cross-site post, which carries only a `SameSite=None` cookie; that needs HTTPS.
        samesite = "none" if secure else "lax"
        way_back = slug if asset is None else f"{slug}/{quote(asset)}"
        response.set_cookie(
            name,
            # Percent-encoded whole, so the cookie holds only characters a cookie value may carry.
            quote(f"{way_back}{query}", safe=""),
            max_age=_RETURN_SECONDS,
            path="/",
            secure=secure,
            httponly=True,
            samesite=samesite,
        )
    return response


def _return_cookie(request: Request) -> tuple[str, bool]:
    """Name the return cookie for this origin, `__Host-` and `Secure` over HTTPS, and say whether it is `Secure`."""
    origin = deployment_origin(api_runtime_paths(request).env_value("MINDROOM_PUBLIC_URL"), str(request.base_url))
    secure = origin is None or not origin.startswith("http://")
    return (f"__Host-{_RETURN_COOKIE}" if secure else _RETURN_COOKIE), secure


def sign_in_landing(request: Request) -> RedirectResponse:
    """Return where a Connections sign-in lands: the report page that sent the browser to sign in, else the portal."""
    name, secure = _return_cookie(request)
    cookie = request.cookies.get(name)
    redirect = RedirectResponse(_way_back(cookie) if cookie else "/connections/", status_code=303)
    if cookie is not None:
        redirect.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="none" if secure else "lax")
    return redirect


def _way_back(cookie: str) -> str:
    """Return the report page a return cookie names, or the portal unless it names a slug and a path inside it."""
    way_back, separator, query = unquote(cookie).partition("?")
    slug, slash, quoted_asset = way_back.partition("/")
    asset = unquote(quoted_asset)
    if not PUBLIC_REPORT_SLUG_RE.fullmatch(slug) or _leaves_report(asset):
        return "/connections/"
    return f"/reports/public/{slug}{slash}{quote(asset)}{separator}{query}"


def _leaves_report(asset: str) -> bool:
    """Return whether a report path has a dot segment or backslash, as the server decodes it or one more decoding reads it."""
    return any(
        "\\" in decoded or any(segment in {".", ".."} for segment in decoded.split("/"))
        for decoded in (asset, unquote(asset))
    )

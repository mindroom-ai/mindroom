# ruff: noqa: D100
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from mindroom.api.auth import (
    dashboard_host_public_origin,
    login_redirect_for_request,
    request_has_frontend_access,
    sanitize_next_path,
)
from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.egress_broker.env import egress_broker_enabled
from mindroom.frontend_assets import ensure_frontend_dist_dir

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

router = APIRouter()

_API_ROUTE_PREFIXES = frozenset({"api", "v1"})


def _resolve_frontend_asset(frontend_dir: Path, request_path: str) -> Path | None:
    """Resolve a request path to a static asset or SPA fallback."""
    normalized_path = unquote(request_path).strip("/")
    index_path = frontend_dir / "index.html"
    if not normalized_path:
        return index_path if index_path.is_file() else None

    candidate_parts = PurePosixPath(normalized_path).parts
    if ".." in candidate_parts:
        return None

    candidate = frontend_dir.joinpath(*candidate_parts)
    if candidate.is_file():
        return candidate

    if candidate.is_dir():
        nested_index_path = candidate / "index.html"
        if nested_index_path.is_file():
            return nested_index_path

    if PurePosixPath(normalized_path).suffix:
        return None

    return index_path if index_path.is_file() else None


def _connections_frontend_enabled(runtime_paths: RuntimePaths, path: str) -> bool:
    """Return whether the dedicated connections bundle may serve one request path: the egress page needs the broker."""
    segments = path.split("/")
    return not (len(segments) > 1 and segments[1] == "egress" and not egress_broker_enabled(runtime_paths))


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend(request: Request, path: str = "") -> Response:
    """Serve the bundled dashboard and SPA routes from the MindRoom runtime."""
    first_segment = path.split("/", 1)[0] if path else ""
    if first_segment in _API_ROUTE_PREFIXES:
        raise HTTPException(status_code=404, detail="Not found")

    # Connections sign-in, account links, and MCP consent belong to the public origin, so their pages move there.
    if first_segment == "connections" and (public := dashboard_host_public_origin(request)) is not None:
        return RedirectResponse(f"{public}{request.url.path}{f'?{request.url.query}' if request.url.query else ''}")

    # The Agent Keys page is absent for everyone while the broker is off, before any login redirect.
    if first_segment == "connections" and not _connections_frontend_enabled(api_runtime_paths(request), path):
        raise HTTPException(status_code=404, detail="Agent Keys are not enabled")

    if not await request_has_frontend_access(request):
        target_path = sanitize_next_path(f"/{path}" if path else "/")
        login_redirect = login_redirect_for_request(request, next_path=target_path)
        if login_redirect is not None:
            return login_redirect
        raise HTTPException(status_code=401, detail="Authentication required")

    runtime_paths = api_runtime_paths(request)
    frontend_dir = ensure_frontend_dist_dir(runtime_paths)
    if frontend_dir is None:
        raise HTTPException(status_code=404, detail="Frontend assets are not available")

    if first_segment == "connections":
        frontend_dir = frontend_dir / "connections"
        path = path.removeprefix("connections").lstrip("/")

    asset_path = _resolve_frontend_asset(frontend_dir, path)
    if asset_path is None:
        raise HTTPException(status_code=404, detail="Frontend asset not found")

    if asset_path.suffix == ".svgz":
        return FileResponse(asset_path, media_type="image/svg+xml", headers={"Content-Encoding": "gzip"})
    return FileResponse(asset_path)

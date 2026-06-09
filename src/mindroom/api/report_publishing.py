"""Public report publishing API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.api.report_headers import set_report_headers
from mindroom.report_publishing.store import ReportPublishingError, ReportPublishingStore

public_router = APIRouter(tags=["report-publishing"])


@public_router.get("/reports/public/{slug}", include_in_schema=False)
async def public_report(request: Request, slug: str) -> Response:
    """Serve one active public HTML report from runtime storage."""
    return _public_report_asset_response(request, slug, None, redirect_static_site_to_slash=True)


@public_router.get("/reports/public/{slug}/", include_in_schema=False)
async def public_report_index(request: Request, slug: str) -> Response:
    """Serve one active public static-site index from runtime storage."""
    return _public_report_asset_response(request, slug, None)


@public_router.get("/reports/public/{slug}/{asset_path:path}", include_in_schema=False)
async def public_report_asset(request: Request, slug: str, asset_path: str) -> Response:
    """Serve one active public static-site asset from runtime storage."""
    return _public_report_asset_response(request, slug, asset_path)


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

    response = FileResponse(report_path)
    set_report_headers(
        response,
        cache_control="no-store, max-age=0",
        sandboxed_static_site=report.is_static_site,
    )
    return response

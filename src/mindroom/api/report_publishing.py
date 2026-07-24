"""Browser-facing report publishing API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from mindroom.api import config_lifecycle
from mindroom.api.auth import verified_matrix_user_id_for_auth_user, verify_report_viewer
from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.api.report_headers import set_report_headers
from mindroom.logging_config import get_logger
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.authorization import ReportAuthorizationReason
from mindroom.report_publishing.store import ReportPublishingError, ReportPublishingStore

if TYPE_CHECKING:
    from pathlib import Path

public_router = APIRouter(tags=["report-publishing"])
logger = get_logger(__name__)
_NOT_FOUND_DETAIL = "Report was not found."
_PUBLIC_NOT_FOUND_DETAIL = "Public report was not found."


@public_router.api_route("/reports/public/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
async def public_report(request: Request, slug: str) -> Response:
    """Serve one active public HTML report from runtime storage."""
    return _public_report_asset_response(request, slug, None, redirect_static_site_to_slash=True)


@public_router.api_route("/reports/public/{slug}/", methods=["GET", "HEAD"], include_in_schema=False)
async def public_report_index(request: Request, slug: str) -> Response:
    """Serve one active public static-site index from runtime storage."""
    return _public_report_asset_response(request, slug, None)


@public_router.api_route(
    "/reports/public/{slug}/{asset_path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def public_report_asset(request: Request, slug: str, asset_path: str) -> Response:
    """Serve one active public static-site asset from runtime storage."""
    return _public_report_asset_response(request, slug, asset_path)


@public_router.api_route("/reports/room/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
async def origin_room_report(request: Request, slug: str) -> Response:
    """Serve one authorized origin-room HTML report."""
    return await _origin_room_report_asset_response(
        request,
        slug,
        None,
        redirect_static_site_to_slash=True,
    )


@public_router.api_route("/reports/room/{slug}/", methods=["GET", "HEAD"], include_in_schema=False)
async def origin_room_report_index(request: Request, slug: str) -> Response:
    """Serve one authorized origin-room static-site index."""
    return await _origin_room_report_asset_response(request, slug, None)


@public_router.api_route(
    "/reports/room/{slug}/{asset_path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def origin_room_report_asset(request: Request, slug: str, asset_path: str) -> Response:
    """Serve one authorized origin-room static-site asset."""
    return await _origin_room_report_asset_response(request, slug, asset_path)


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
        report = store.get_report(slug)
    except ReportPublishingError as exc:
        raise HTTPException(status_code=404, detail=_PUBLIC_NOT_FOUND_DETAIL) from exc
    if report.access_policy is not ReportAccessPolicy.PUBLIC:
        raise HTTPException(status_code=404, detail=_PUBLIC_NOT_FOUND_DETAIL)
    if report.is_static_site and redirect_static_site_to_slash:
        # Relative-URL assets only resolve under the trailing-slash form,
        # and a relative Location keeps any subpath proxy prefix intact.
        return RedirectResponse(url=f"{slug}/", status_code=301)
    try:
        report_path = store.report_asset_path(report, asset_path)
    except ReportPublishingError as exc:
        raise HTTPException(status_code=404, detail=_PUBLIC_NOT_FOUND_DETAIL) from exc

    return _report_file_response(report_path, sandboxed_static_site=report.is_static_site)


async def _origin_room_report_asset_response(
    request: Request,
    slug: str,
    asset_path: str | None,
    *,
    redirect_static_site_to_slash: bool = False,
) -> Response:
    auth_user = await verify_report_viewer(request)
    viewer_matrix_user_id = verified_matrix_user_id_for_auth_user(auth_user)
    if viewer_matrix_user_id is None:
        _log_report_authorization(
            outcome="matrix_identity_missing",
            asset_path=asset_path,
        )
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    runtime_paths = api_runtime_paths(request)
    store = ReportPublishingStore(runtime_paths.storage_root)
    try:
        report = store.get_report(slug)
    except ReportPublishingError as exc:
        _log_report_authorization(
            outcome="report_not_found",
            asset_path=asset_path,
        )
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL) from exc
    if report.access_policy is not ReportAccessPolicy.ORIGIN_ROOM:
        _log_report_authorization(
            outcome="report_not_found",
            asset_path=asset_path,
        )
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    runtime = config_lifecycle.app_state(request.app).report_authorization_runtime
    if runtime is None:
        _log_report_authorization(
            outcome=ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE.value,
            asset_path=asset_path,
            publisher_entity_name=report.publisher_entity_name,
        )
        raise HTTPException(status_code=503, detail="Report authorization is temporarily unavailable.")
    try:
        decision = await runtime.authorize(report, viewer_matrix_user_id)
    except Exception as exc:  # fail closed at runtime callback boundary
        logger.exception(
            "report_authorization_failed",
            access_policy=ReportAccessPolicy.ORIGIN_ROOM.value,
            outcome=ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE.value,
            request_type=_request_type(asset_path),
        )
        raise HTTPException(status_code=503, detail="Report authorization is temporarily unavailable.") from exc

    _log_report_authorization(
        outcome=decision.reason.value,
        asset_path=asset_path,
        cache_hit=decision.cache_hit,
        publisher_entity_name=report.publisher_entity_name,
    )
    if decision.backend_unavailable:
        raise HTTPException(status_code=503, detail="Report authorization is temporarily unavailable.")
    if not decision.authorized:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    if report.is_static_site and redirect_static_site_to_slash:
        # Relative-URL assets only resolve under the trailing-slash form,
        # and a relative Location keeps any subpath proxy prefix intact.
        return RedirectResponse(url=f"{slug}/", status_code=301)
    try:
        report_path = store.report_asset_path(report, asset_path)
    except ReportPublishingError as exc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL) from exc
    return _report_file_response(report_path, sandboxed_static_site=report.is_static_site)


def _report_file_response(report_path: Path, *, sandboxed_static_site: bool) -> FileResponse:
    response = FileResponse(report_path)
    set_report_headers(
        response,
        cache_control="no-store, max-age=0",
        sandboxed_static_site=sandboxed_static_site,
    )
    return response


def _request_type(asset_path: str | None) -> str:
    return "root" if asset_path in (None, "") else "asset"


def _log_report_authorization(
    *,
    outcome: str,
    asset_path: str | None,
    cache_hit: bool | None = None,
    publisher_entity_name: str | None = None,
) -> None:
    logger.info(
        "report_authorization",
        access_policy=ReportAccessPolicy.ORIGIN_ROOM.value,
        outcome=outcome,
        request_type=_request_type(asset_path),
        cache_hit=cache_hit,
        publisher_entity_name=publisher_entity_name,
    )

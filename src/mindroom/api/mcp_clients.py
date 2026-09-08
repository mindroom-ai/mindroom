"""Signed personal management of external MCP client connections."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from mindroom.api.auth import require_personal_connections_user
from mindroom.api.config_lifecycle import rebind_current_request_snapshot
from mindroom.api.personal_agent import PERSONAL_RESPONSE_HEADERS
from mindroom.mcp_gateway.server import read_gateway_body
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from mindroom.mcp_gateway.oauth import GatewayOAuthProvider


class _ClientRuntime(Protocol):
    @property
    def provider(self) -> GatewayOAuthProvider: ...

    @property
    def origin(self) -> str: ...


async def _list_clients(request: Request, runtime: _ClientRuntime, owner: dict[str, str]) -> JSONResponse:
    if set(request.query_params) - {"cursor"} or len(request.query_params.getlist("cursor")) > 1:
        raise HTTPException(400, "Client target overrides are not accepted")
    cursor = request.query_params.get("cursor")
    if cursor is not None and (not cursor or len(cursor) > 256):
        raise HTTPException(400, "Invalid client page")
    grants = await runtime.provider.list_grants(**owner, after=cursor, limit=101)
    return JSONResponse(
        {"enabled": True, "clients": grants[:100], "next_cursor": grants[99]["id"] if len(grants) > 100 else None},
        headers=PERSONAL_RESPONSE_HEADERS,
    )


async def _revoke_clients(request: Request, runtime: _ClientRuntime, owner: dict[str, str]) -> JSONResponse:
    if request.headers.get("origin") != runtime.origin or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Client changes require a same-origin request")
    if request.query_params:
        raise HTTPException(400, "Client target overrides are not accepted")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON content required")
    body = await read_gateway_body(request)
    try:
        mutation = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid client change") from exc
    if mutation != {}:
        raise HTTPException(400, "Client target overrides are not accepted")
    grant_id = request.path_params.get("grant_id")
    if grant_id is not None and len(grant_id) > 256:
        raise HTTPException(404, "Client connection not found")
    revoked = await runtime.provider.revoke_grants(**owner, grant_id=grant_id)
    if grant_id is not None and not revoked:
        raise HTTPException(404, "Client connection not found")
    return JSONResponse({"success": True}, headers=PERSONAL_RESPONSE_HEADERS)


async def _handle_clients(request: Request, runtime_for_request: Callable[[Request], _ClientRuntime]) -> JSONResponse:
    user = await require_personal_connections_user(request)
    try:
        runtime = runtime_for_request(request)
    except HTTPException as exc:
        if exc.status_code == 404 and request.method in {"GET", "HEAD"}:
            return JSONResponse({"enabled": False, "clients": []}, headers=PERSONAL_RESPONSE_HEADERS)
        raise
    snapshot = rebind_current_request_snapshot(request)
    agent_name = (snapshot.runtime_paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name or snapshot.runtime_config is None:
        raise HTTPException(503, "Client connections are unavailable")
    authenticated_user_id = user["matrix_user_id"]
    requester_id = resolve_human_requester_alias(authenticated_user_id, snapshot.runtime_config, snapshot.runtime_paths)
    owner = {"requester_id": requester_id, "authenticated_user_id": authenticated_user_id, "agent_name": agent_name}
    if request.method in {"GET", "HEAD"}:
        return await _list_clients(request, runtime, owner)
    return await _revoke_clients(request, runtime, owner)


def client_routes(runtime_for_request: Callable[[Request], _ClientRuntime]) -> list[Route]:
    """Install private controls without importing the runtime composition root."""

    async def clients(request: Request) -> JSONResponse:
        try:
            return await _handle_clients(request, runtime_for_request)
        except HTTPException as exc:
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={**(exc.headers or {}), **PERSONAL_RESPONSE_HEADERS},
            )
        except TimeoutError:
            return JSONResponse({"detail": "Request timed out"}, status_code=408, headers=PERSONAL_RESPONSE_HEADERS)

    return [
        Route("/api/connections/mcp/clients", clients, methods=["GET"]),
        Route("/api/connections/mcp/clients/revoke-all", clients, methods=["POST"]),
        Route("/api/connections/mcp/clients/{grant_id}/revoke", clients, methods=["POST"]),
    ]

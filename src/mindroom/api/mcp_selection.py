"""Signed Connections controls for the user's selection across all MCP clients."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from mindroom.api.auth import require_connections_user
from mindroom.api.config_lifecycle import rebind_current_request_snapshot
from mindroom.api.connection_agents import CONNECTIONS_HEADERS, resolve_connection_user
from mindroom.api.mcp_identity import resolve_gateway_browser_owner
from mindroom.mcp_gateway.selection import SelectionAccessDeniedError
from mindroom.mcp_gateway.server import read_gateway_body
from mindroom.mcp_gateway.store import GatewayOAuthCapacityError

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from mindroom.mcp_gateway.oauth import GatewayOAuthProvider
    from mindroom.mcp_gateway.selection import GatewaySelections


class _SelectionRuntime(Protocol):
    @property
    def provider(self) -> GatewayOAuthProvider: ...

    @property
    def origin(self) -> str: ...

    @property
    def selections(self) -> GatewaySelections: ...


async def _selection_names(request: Request, origin: str) -> tuple[str, ...]:
    if request.headers.get("origin") != origin or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Selection changes require a same-origin request")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON content required")
    try:
        mutation = json.loads(await read_gateway_body(request))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid agent selection") from exc
    if (
        not isinstance(mutation, dict)
        or set(mutation) != {"agents"}
        or not isinstance(mutation["agents"], list)
        or any(not isinstance(name, str) for name in mutation["agents"])
    ):
        raise HTTPException(400, "Invalid agent selection")
    return tuple(mutation["agents"])


async def _handle_selection(
    request: Request,
    runtime_for_request: Callable[[Request], _SelectionRuntime],
) -> JSONResponse:
    user = await require_connections_user(request)
    try:
        runtime = runtime_for_request(request)
    except HTTPException as exc:
        if exc.status_code == 404 and request.method in {"GET", "HEAD"}:
            return JSONResponse({"enabled": False, "selected_agents": []}, headers=CONNECTIONS_HEADERS)
        raise
    if request.query_params:
        raise HTTPException(400, "Selection target overrides are not accepted")
    names = await _selection_names(request, runtime.origin) if request.method == "POST" else None
    owner = await resolve_gateway_browser_owner(request, user, runtime.provider)
    context = resolve_connection_user(
        rebind_current_request_snapshot(request),
        owner.authenticated_user_id,
        account_id=owner.account_id,
    )
    runtime_for_request(request)
    if names is None:
        defaults = (context.personal_agent_name,) if context.personal_agent_name is not None else ()
        saved = await runtime.selections.get(context.owner, defaults)
    else:
        if any(name not in context.agent_names for name in names):
            raise HTTPException(404, "Agent is not available")
        try:
            saved = await runtime.selections.set(context.owner, names)
        except ValueError as exc:
            raise HTTPException(400, "Invalid agent selection") from exc
    return JSONResponse(
        {"enabled": True, "selected_agents": [name for name in saved if name in context.agent_names]},
        headers=CONNECTIONS_HEADERS,
    )


def selection_routes(runtime_for_request: Callable[[Request], _SelectionRuntime]) -> list[Route]:
    """Install settings for local and external authentication without importing the runtime root."""

    async def selection(request: Request) -> JSONResponse:
        try:
            return await _handle_selection(request, runtime_for_request)
        except HTTPException as exc:
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=CONNECTIONS_HEADERS,
            )
        except SelectionAccessDeniedError:
            return JSONResponse(
                {"detail": "Account access has changed"},
                status_code=403,
                headers=CONNECTIONS_HEADERS,
            )
        except GatewayOAuthCapacityError:
            return JSONResponse(
                {"detail": "Selection storage is temporarily full"},
                status_code=503,
                headers=CONNECTIONS_HEADERS,
            )
        except TimeoutError:
            return JSONResponse({"detail": "Request timed out"}, status_code=408, headers=CONNECTIONS_HEADERS)

    return [Route("/api/connections/mcp/selection", selection, methods=["GET", "POST"])]

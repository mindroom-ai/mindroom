"""Verified browser ownership shared by gateway selection, consent and client controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from mindroom.api.config_lifecycle import rebind_current_request_snapshot
from mindroom.mcp_gateway.accounts import GatewayAccounts
from mindroom.mcp_gateway.types import GatewayOwner
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from starlette.requests import Request

    from mindroom.mcp_gateway.oauth import GatewayOAuthProvider


async def resolve_gateway_browser_owner(
    request: Request,
    user: dict[str, Any],
    provider: GatewayOAuthProvider,
) -> GatewayOwner:
    """Bind signed login to its current provisioned account without requiring agent access."""
    account_id = None
    if provider.accounts_required:
        email = user.get("email")
        account_id = await GatewayAccounts(provider.store).resolve_active(email) if isinstance(email, str) else None
        if account_id is None:
            raise HTTPException(403, "An active provisioned account is required")
    snapshot = rebind_current_request_snapshot(request)
    if snapshot.runtime_config is None:
        raise HTTPException(503, "Client connections are unavailable")
    authenticated_user_id = user["matrix_user_id"]
    requester_id = resolve_human_requester_alias(authenticated_user_id, snapshot.runtime_config, snapshot.runtime_paths)
    return GatewayOwner(authenticated_user_id, requester_id, account_id)

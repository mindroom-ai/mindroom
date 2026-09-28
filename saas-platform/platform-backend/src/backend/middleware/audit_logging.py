"""Audit logging middleware for accepted state-changing requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from backend.utils.audit import AuditActor
    from fastapi import Request, Response

from backend.config import logger, supabase
from backend.deps import client_ip_from_request
from backend.utils.audit import redact_audit_details
from starlette.middleware.base import BaseHTTPMiddleware


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Write an audit row for every state-changing request that a route accepted with a 2xx status.

    Rows hold request metadata only: the method, path, status, the account an auth dependency verified,
    and the client IP. Request bodies are never read here; routes that need their data in the audit log,
    such as the admin routes, write explicit entries.
    """

    AUDIT_METHODS: ClassVar[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    # Path fragments mapped to resource types; the first match wins.
    RESOURCE_TYPES: ClassVar[dict[str, str]] = {
        "account": "account",
        "gdpr": "account",
        "subscription": "subscription",
        "instance": "instance",
        "provision": "instance",
        "stripe": "payment",
        "sso": "authentication",
        "oidc": "authentication",
        "admin": "admin_action",
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log it when a route accepted a state-changing request."""
        if request.method not in self.AUDIT_METHODS:
            return await call_next(request)

        request.state.audit_actor = None
        response = await call_next(request)
        if not 200 <= response.status_code < 300:
            return response

        path = request.url.path
        actor: AuditActor | None = request.state.audit_actor
        await self._create_audit_log(
            account_id=actor.account_id if actor else None,
            action=self._get_action(request.method),
            resource_type=self._get_resource_type(path),
            resource_id=self._extract_resource_id(path),
            ip_address=client_ip_from_request(request),
            details={
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "user_email": actor.email if actor else None,
            },
        )
        return response

    def _get_action(self, method: str) -> str:
        """Map HTTP method to action name."""
        mapping = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}
        return mapping.get(method, method.lower())

    def _get_resource_type(self, path: str) -> str:
        """Extract resource type from path."""
        for key, resource_type in self.RESOURCE_TYPES.items():
            if key in path:
                return resource_type
        return "unknown"

    def _extract_resource_id(self, path: str) -> str | None:
        """Extract resource ID from path if present."""
        parts = path.strip("/").split("/")
        # Look for UUID-like strings or numeric IDs
        for part in parts:
            if "-" in part and len(part) > 20:  # Likely a UUID
                return part
            try:
                # Check if it's a numeric ID
                int(part)
            except ValueError:
                continue
            else:
                return part
        return None

    async def _create_audit_log(
        self,
        *,
        account_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        ip_address: str,
        details: dict[str, Any],
    ) -> None:
        """Create audit log entry in database."""
        try:
            if not supabase:
                return

            log_entry = {
                "account_id": account_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": redact_audit_details({key: value for key, value in details.items() if value is not None}),
                "ip_address": ip_address,
                "created_at": datetime.now(UTC).isoformat(),
            }

            # Remove None values
            log_entry = {k: v for k, v in log_entry.items() if v is not None}

            supabase.table("audit_logs").insert(log_entry).execute()
        except Exception as e:
            logger.error(f"Failed to create audit log: {e}")
            # Let audit failures be visible but don't block the request

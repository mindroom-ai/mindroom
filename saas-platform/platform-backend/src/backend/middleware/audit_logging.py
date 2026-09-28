"""Audit logging middleware for successful state-changing requests."""

from __future__ import annotations

import json
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

# Larger bodies are recorded by a marker instead of being buffered and parsed for the audit row.
AUDIT_BODY_MAX_BYTES = 8 * 1024


async def _audited_body(request: Request) -> bytes | None:
    """Return the request body when its declared length fits the audit cap, or None when it is not captured."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        return None if "transfer-encoding" in request.headers else b""
    if not content_length.isdigit() or int(content_length) > AUDIT_BODY_MAX_BYTES:
        return None
    return await request.body()


def _body_details(body: bytes | None) -> Any:  # noqa: ANN401
    if body is None:
        return {"body": "not-captured"}
    if not body:
        return {}
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return {"body": "non-json"}


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Write an audit row for every state-changing request that a route accepted.

    Body parsing and redaction run only after the route answered with a success status,
    so unauthenticated and unrouted requests cost no audit work beyond buffering a small body.
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

        body = await _audited_body(request)
        request.state.audit_actor = None
        response = await call_next(request)
        if response.status_code >= 400:
            return response

        path = request.url.path
        actor: AuditActor | None = request.state.audit_actor
        await self._create_audit_log(
            account_id=actor.account_id if actor else None,
            action=self._get_action(request.method),
            resource_type=self._get_resource_type(path),
            resource_id=self._extract_resource_id(path),
            details=_body_details(body),
            ip_address=client_ip_from_request(request),
            user_email=actor.email if actor else None,
            path=path,
            status_code=response.status_code,
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
        account_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: Any,  # noqa: ANN401
        ip_address: str | None,
        user_email: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
    ) -> None:
        """Create audit log entry in database."""
        try:
            if not supabase:
                return

            normalized_details = details if isinstance(details, dict) else {"body": details}
            log_entry = {
                "account_id": account_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": redact_audit_details(
                    {**normalized_details, "path": path, "status_code": status_code, "user_email": user_email}
                ),
                "ip_address": ip_address,
                "created_at": datetime.now(UTC).isoformat(),
            }

            # Remove None values
            log_entry = {k: v for k, v in log_entry.items() if v is not None}

            supabase.table("audit_logs").insert(log_entry).execute()
        except Exception as e:
            logger.error(f"Failed to create audit log: {e}")
            # Let audit failures be visible but don't block the request

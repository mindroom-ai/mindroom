"""Audit logging middleware for tracking admin and user actions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request, Response

from backend.config import logger, supabase
from backend.tasks.usage_metrics import update_realtime_metrics
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log important actions to audit_logs table."""

    # Actions to audit
    AUDIT_METHODS: ClassVar[set[str]] = {"POST", "PUT", "PATCH", "DELETE"}

    # Path patterns to audit
    AUDIT_PATHS: ClassVar[list[str]] = [
        "/admin/",  # All admin actions
        "/api/accounts",  # Account modifications
        "/api/subscriptions",  # Subscription changes
        "/api/instances",  # Instance management
        "/stripe/",  # Stripe webhooks
    ]

    # Map of path patterns to resource types
    RESOURCE_TYPES: ClassVar[dict[str, str]] = {
        "accounts": "account",
        "subscriptions": "subscription",
        "instances": "instance",
        "admin": "admin_action",
        "stripe": "payment",
        "auth": "authentication",
    }

    async def dispatch(  # noqa: C901
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        """Process request and log if needed."""
        # Skip non-auditable requests
        if request.method not in self.AUDIT_METHODS:
            return await call_next(request)

        # Check if path should be audited
        path = str(request.url.path)
        should_audit = any(pattern in path for pattern in self.AUDIT_PATHS)

        if not should_audit:
            return await call_next(request)

        # Get user from request state if available
        # Note: Authentication happens at route level via dependencies,
        # so these may not be set yet in the middleware
        user_id = getattr(request.state, "user_id", None)
        user_email = getattr(request.state, "user_email", None)

        # Determine action and resource type
        action = self._get_action(request.method)
        resource_type = self._get_resource_type(path)
        resource_id = self._extract_resource_id(path)

        # Get request body for details (be careful with sensitive data)
        details = {}
        if request.method in {"POST", "PUT", "PATCH"}:
            try:
                # Store the body for later use
                body = await request.body()
                # Recreate the request with the stored body
                # Create a new request with the body we read
                request = StarletteRequest(
                    scope={
                        **request.scope,
                        "body": body,
                    },
                    receive=request.receive,
                    send=request._send,
                )

                # Parse body for audit details (remove sensitive fields)
                if body:
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        details = {"body": "non-json"}
                    else:
                        # Remove sensitive fields
                        sensitive_fields = {
                            "password",
                            "api_key",
                            "secret",
                            "token",
                            "credit_card",
                        }
                        details = {
                            k: v
                            for k, v in data.items()
                            if k.lower() not in sensitive_fields
                            and not any(sensitive in k.lower() for sensitive in sensitive_fields)
                        }
            except Exception as e:
                logger.warning(f"Failed to parse request body for audit: {e}")

        # Get client IP
        client_ip = request.client.host if request.client else None

        # Process the request
        response = await call_next(request)

        # Only log successful operations
        if response.status_code < 400:
            await self._create_audit_log(
                account_id=user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
                ip_address=client_ip,
                user_email=user_email,
                path=path,
                status_code=response.status_code,
            )

            # Track API usage metrics
            if user_id:
                await update_realtime_metrics(user_id, "api_calls", 1)

                # Track specific actions as messages
                if resource_type == "message" or action in ["send", "message", "chat"]:
                    await update_realtime_metrics(user_id, "messages_sent", 1)

        return response

    def _get_action(self, method: str) -> str:
        """Map HTTP method to action name."""
        mapping = {
            "POST": "create",
            "PUT": "update",
            "PATCH": "update",
            "DELETE": "delete",
        }
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
        details: dict[str, Any],
        ip_address: str | None,
        user_email: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
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
                "details": {
                    **details,
                    "path": path,
                    "status_code": status_code,
                    "user_email": user_email,
                },
                "ip_address": ip_address,
                "created_at": datetime.now(UTC).isoformat(),
            }

            # Remove None values
            log_entry = {k: v for k, v in log_entry.items() if v is not None}

            supabase.table("audit_logs").insert(log_entry).execute()
        except Exception as e:
            logger.error(f"Failed to create audit log: {e}")
            # Let audit failures be visible but don't block the request

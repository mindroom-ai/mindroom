"""Unified Google Integration for MindRoom.

This module provides a single, comprehensive Google OAuth integration supporting:
- Gmail (read, compose, modify)
- Google Calendar (events, scheduling)
- Google Drive (file access)

Replaces the previous fragmented gmail_config.py, google_auth.py, and google_setup_wizard.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mindroom.api import config_lifecycle
from mindroom.api.credentials import (
    RequestCredentialsTarget,
    consume_pending_oauth_request,
    issue_pending_oauth_state,
    load_credentials_for_target,
    resolve_request_credentials_target,
)
from mindroom.connections import (
    canonical_connection_provider,
    connection_oauth_client,
    default_connection_id,
    resolve_connection,
)
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.tool_system.dependencies import ensure_tool_deps
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow

    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

router = APIRouter(prefix="/api/google", tags=["google-integration"])

# OAuth scopes for all Google services needed by MindRoom
_SCOPES = [
    # Gmail
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    # Calendar
    "https://www.googleapis.com/auth/calendar",
    # Sheets
    "https://www.googleapis.com/auth/spreadsheets",
    # Drive
    "https://www.googleapis.com/auth/drive.file",
    # User info
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

_GOOGLE_OAUTH_DEPS = ["google-auth", "google-auth-oauthlib"]
_GOOGLE_OAUTH_NOT_CONFIGURED_MESSAGE = (
    "Google OAuth is not configured. Please configure a google/oauth client connection first."
)


class GoogleOAuthNotConfiguredError(ValueError):
    """Raised when the dashboard cannot find a google/oauth client connection."""


def _mindroom_port(runtime_paths: RuntimePaths) -> str:
    return runtime_paths.env_value("MINDROOM_PORT", default="8765") or "8765"


def _redirect_uri(runtime_paths: RuntimePaths) -> str:
    default_redirect_uri = f"http://localhost:{_mindroom_port(runtime_paths)}/api/google/callback"
    return runtime_paths.env_value("GOOGLE_REDIRECT_URI", default=default_redirect_uri) or default_redirect_uri


def _ensure_google_packages(runtime_paths: RuntimePaths) -> tuple[type[GoogleRequest], type[Credentials], type[Flow]]:
    """Lazily import Google auth packages, auto-installing if needed."""
    ensure_tool_deps(_GOOGLE_OAUTH_DEPS, "gmail", runtime_paths)

    from google.auth.transport.requests import Request  # noqa: PLC0415
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from google_auth_oauthlib.flow import Flow  # noqa: PLC0415

    return Request, Credentials, Flow


class GoogleStatus(BaseModel):
    """Google integration status."""

    connected: bool
    email: str | None = None
    services: list[str] = []
    error: str | None = None
    has_credentials: bool = False


class GoogleAuthUrl(BaseModel):
    """Google OAuth URL response."""

    auth_url: str


def _get_oauth_credentials(
    runtime_paths: RuntimePaths,
    *,
    config: Config,
) -> dict[str, Any] | None:
    """Get OAuth credentials from the shared google/oauth connection."""
    oauth_client = _google_oauth_client_pair(runtime_paths, config=config)
    if oauth_client is None:
        return None
    client_id, client_secret = oauth_client

    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [_redirect_uri(runtime_paths)],
        },
    }


def _legacy_google_oauth_client_pair(runtime_paths: RuntimePaths) -> tuple[str, str] | None:
    """Return the legacy shared Google OAuth client payload when present."""
    credentials = get_runtime_credentials_manager(runtime_paths).shared_manager().load_credentials("google_oauth_client")
    if not isinstance(credentials, dict):
        return None
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        return None
    normalized_client_id = client_id.strip()
    normalized_client_secret = client_secret.strip()
    if not normalized_client_id or not normalized_client_secret:
        return None
    return normalized_client_id, normalized_client_secret


def _google_oauth_client_pair(
    runtime_paths: RuntimePaths,
    *,
    config: Config,
) -> tuple[str, str] | None:
    """Resolve the active Google OAuth client, keeping env-seeded legacy fallbacks working."""
    try:
        resolved_connection = resolve_connection(
            config,
            provider="google",
            purpose="google_oauth_client",
            runtime_paths=runtime_paths,
        )
    except ValueError:
        return _legacy_google_oauth_client_pair(runtime_paths)
    oauth_client = connection_oauth_client(resolved_connection)
    return oauth_client or _legacy_google_oauth_client_pair(runtime_paths)


def _require_oauth_credentials(
    runtime_paths: RuntimePaths,
    *,
    config: Config,
) -> dict[str, Any]:
    """Return Google OAuth credentials or raise one consistent API error."""
    oauth_config = _get_oauth_credentials(runtime_paths, config=config)
    if oauth_config is None:
        raise HTTPException(status_code=503, detail="OAuth not configured")
    return oauth_config


def _build_google_token_data(creds: Credentials) -> dict[str, Any]:
    """Convert Google credentials to the stored token payload."""
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": creds.scopes,
        "_source": "ui",
    }

    id_token = creds.id_token
    if id_token:
        token_data["_id_token"] = id_token
    return token_data


def _get_google_credentials(
    target: RequestCredentialsTarget,
    runtime_paths: RuntimePaths,
    *,
    config: Config,
) -> Credentials | None:
    """Get Google credentials from stored token."""
    token_data = load_credentials_for_target("google", target)
    if not token_data:
        return None

    try:
        google_request_cls, credentials_cls, _ = _ensure_google_packages(runtime_paths)
        oauth_client = _google_oauth_client_pair(runtime_paths, config=config)
        creds = credentials_cls(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=oauth_client[0] if oauth_client is not None else token_data.get("client_id"),
            client_secret=oauth_client[1] if oauth_client is not None else token_data.get("client_secret"),
            scopes=token_data.get("scopes", _SCOPES),
        )

        # Refresh token if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google_request_cls())
            # Save refreshed credentials
            _save_credentials(creds, target)
    except Exception:
        return None
    else:
        return creds if creds and creds.valid else None


def _save_credentials(creds: Credentials, target: RequestCredentialsTarget) -> None:
    """Save credentials using the unified credentials manager."""
    save_scoped_credentials(
        "google",
        _build_google_token_data(creds),
        credentials_manager=target.base_manager,
        worker_target=resolve_worker_target(
            target.worker_scope,
            target.agent_name,
            execution_identity=target.execution_identity,
        ),
    )


def _google_oauth_client_service(config: Config) -> str:
    """Return the backing credential service for the configured google/oauth connection."""
    connection_id = default_connection_id(provider="google", purpose="google_oauth_client")
    if connection_id is None:
        raise GoogleOAuthNotConfiguredError(_GOOGLE_OAUTH_NOT_CONFIGURED_MESSAGE)

    connection_config = config.connections.get(connection_id)
    if connection_config is None:
        raise GoogleOAuthNotConfiguredError(_GOOGLE_OAUTH_NOT_CONFIGURED_MESSAGE)
    if canonical_connection_provider(connection_config.provider) != "google":
        msg = f"Google OAuth connection '{connection_id}' must use provider 'google'"
        raise ValueError(msg)
    if connection_config.auth_kind != "oauth_client":
        msg = f"Google OAuth connection '{connection_id}' must use auth_kind 'oauth_client'"
        raise ValueError(msg)
    if connection_config.service is None:
        msg = "Google OAuth client connection is missing its backing credential service"
        raise ValueError(msg)
    return connection_config.service


def _require_request_snapshot(request: Request) -> ApiSnapshot:
    """Return the auth-bound API snapshot for one protected dashboard request."""
    from mindroom.api.main import request_api_snapshot  # noqa: PLC0415

    snapshot = request_api_snapshot(request)
    if snapshot is None:
        msg = "Authenticated request is missing its bound API snapshot"
        raise RuntimeError(msg)
    return snapshot


def _save_oauth_client_credentials(
    client_id: str,
    client_secret: str,
    runtime_paths: RuntimePaths,
    *,
    config: Config,
) -> RuntimePaths:
    """Save shared Google OAuth client credentials."""
    get_runtime_credentials_manager(runtime_paths).shared_manager().save_credentials(
        _google_oauth_client_service(config),
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "_source": "ui",
        },
    )
    return runtime_paths


def _reset_google_credentials(
    runtime_paths: RuntimePaths,
    *,
    oauth_client_services: set[str],
) -> RuntimePaths:
    """Clear shared Google OAuth client credentials and persisted tokens."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    shared_manager = credentials_manager.shared_manager()
    for oauth_client_service in sorted(oauth_client_services | {"google_oauth_client"}):
        shared_manager.delete_credentials(oauth_client_service)
    credentials_manager.delete_credentials("google")
    if shared_manager.base_path != credentials_manager.base_path:
        shared_manager.delete_credentials("google")
    workers_root = runtime_paths.storage_root / "workers"
    if workers_root.exists():
        for credentials_path in workers_root.glob("*/credentials/google_credentials.json"):
            credentials_path.unlink(missing_ok=True)
        for credentials_path in workers_root.glob("*/.shared_credentials/google_credentials.json"):
            credentials_path.unlink(missing_ok=True)
    return runtime_paths


def _google_oauth_client_services(config: Config) -> set[str]:
    """Return every configured Google OAuth client backing service."""
    return {
        connection.service
        for connection in config.connections.values()
        if connection.service is not None
        and connection.auth_kind == "oauth_client"
        and canonical_connection_provider(connection.provider) == "google"
    }


@router.get("/status")
async def get_status(request: Request, agent_name: str | None = None) -> GoogleStatus:
    """Check Google integration status."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
    runtime_paths = target.runtime_paths
    config, _runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    has_credentials = _get_oauth_credentials(runtime_paths, config=config) is not None

    creds = _get_google_credentials(target, runtime_paths, config=config)

    if not creds:
        return GoogleStatus(
            connected=False,
            has_credentials=has_credentials,
        )

    try:
        # Check which services are accessible based on scopes
        services = []
        if creds.has_scopes(["https://www.googleapis.com/auth/gmail.modify"]):
            services.append("Gmail")
        if creds.has_scopes(["https://www.googleapis.com/auth/calendar"]):
            services.append("Google Calendar")
        if creds.has_scopes(["https://www.googleapis.com/auth/spreadsheets"]):
            services.append("Google Sheets")
        if creds.has_scopes(["https://www.googleapis.com/auth/drive.file"]):
            services.append("Google Drive")

        # Get user email from token
        email = None
        try:
            id_token = creds.id_token
            if id_token:
                decoded = jwt.decode(id_token, options={"verify_signature": False})
                email = decoded.get("email")
        except Exception:
            email = None

        return GoogleStatus(
            connected=True,
            email=email,
            services=services,
            has_credentials=has_credentials,
        )
    except Exception as e:
        return GoogleStatus(
            connected=False,
            error=str(e),
            has_credentials=has_credentials,
        )


@router.post("/connect")
async def connect(request: Request, agent_name: str | None = None) -> GoogleAuthUrl:
    """Start Google OAuth flow."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
    runtime_paths = target.runtime_paths
    config, _runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    oauth_config = _get_oauth_credentials(runtime_paths, config=config)
    if not oauth_config:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Please configure a google/oauth client connection first.",
        )

    try:
        _, _, flow_cls = _ensure_google_packages(runtime_paths)
        state = issue_pending_oauth_state(request, "google", agent_name)

        # Create OAuth flow with all scopes
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = _redirect_uri(runtime_paths)
        flow = flow_cls.from_client_config(oauth_config, scopes=_SCOPES, redirect_uri=current_redirect_uri)

        # Generate authorization URL
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )

        return GoogleAuthUrl(auth_url=auth_url)
    except HTTPException:
        raise
    except ImportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start Google OAuth: {e!s}") from e


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Google OAuth callback."""
    # Get the authorization code from the callback
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")

    state = request.query_params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="No OAuth state received")

    from mindroom.api.main import verify_user  # noqa: PLC0415

    await verify_user(request, request.headers.get("authorization"), allow_public_paths=False)
    pending = consume_pending_oauth_request(request, "google", state)
    agent_name = pending.agent_name

    try:
        target = resolve_request_credentials_target(
            request,
            agent_name=agent_name,
            service_names=("google",),
            execution_scope_override_provided=pending.execution_scope_override_provided,
            execution_scope_override=pending.execution_scope_override,
        )
        runtime_paths = target.runtime_paths
        config, _runtime_paths = config_lifecycle.read_committed_runtime_config(request)
        oauth_config = _require_oauth_credentials(runtime_paths, config=config)
        _, _, flow_cls = _ensure_google_packages(runtime_paths)

        # Create OAuth flow and exchange code for tokens
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = _redirect_uri(runtime_paths)
        flow = flow_cls.from_client_config(oauth_config, scopes=_SCOPES, redirect_uri=current_redirect_uri)
        flow.fetch_token(code=code)

        # Save credentials
        _save_credentials(flow.credentials, target)

        # Extract the domain from the redirect URI for the final redirect
        parsed_uri = urlparse(current_redirect_uri)
        base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
        return RedirectResponse(url=f"{base_url}/?google=connected")
    except HTTPException:
        raise
    except ImportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        # Check if it's a scope change error
        error_msg = str(e)
        if "Scope has changed" in error_msg:
            raise HTTPException(
                status_code=400,
                detail=f"OAuth scope mismatch: {error_msg}. Please disconnect and reconnect to authorize with the new scopes.",
            ) from e
        raise HTTPException(status_code=500, detail=f"OAuth callback failed: {error_msg}") from e


@router.post("/disconnect")
async def disconnect(request: Request, agent_name: str | None = None) -> dict[str, str]:
    """Disconnect Google services by removing stored tokens."""
    try:
        target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
        target.target_manager.delete_credentials("google")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disconnect: {e!s}") from e
    else:
        return {"status": "disconnected"}


@router.post("/configure")
async def configure(request: Request, credentials: dict[str, str]) -> dict[str, Any]:
    """Configure Google OAuth credentials manually."""
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="client_id and client_secret are required",
        )

    try:
        from mindroom.api.main import _reload_api_runtime_config  # noqa: PLC0415

        config, _runtime_paths = config_lifecycle.read_committed_runtime_config(request)
        snapshot = _require_request_snapshot(request)
        _reload_api_runtime_config(
            request.app,
            api_runtime_paths(request),
            expected_snapshot=snapshot,
            mutate_runtime=lambda runtime_paths: _save_oauth_client_credentials(
                client_id,
                client_secret,
                runtime_paths,
                config=config,
            ),
        )
    except GoogleOAuthNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save credentials: {e!s}") from e
    return {"success": True, "message": "Google OAuth credentials configured successfully"}


@router.post("/reset")
async def reset(request: Request) -> dict[str, Any]:
    """Reset Google integration by removing all credentials and tokens."""
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    oauth_client_services: set[str] = set()
    try:
        from mindroom.api.main import _reload_api_runtime_config  # noqa: PLC0415

        config, _runtime_paths = config_lifecycle.read_committed_runtime_config(request)
        snapshot = _require_request_snapshot(request)
        oauth_client_services = _google_oauth_client_services(config)
        _reload_api_runtime_config(
            request.app,
            api_runtime_paths(request),
            expected_snapshot=snapshot,
            mutate_runtime=lambda runtime_paths: _reset_google_credentials(
                runtime_paths,
                oauth_client_services=oauth_client_services,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset: {e!s}") from e
    return {"success": True, "message": "Google integration reset successfully"}

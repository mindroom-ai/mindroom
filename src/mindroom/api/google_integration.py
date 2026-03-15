"""Unified Google Integration for MindRoom.

This module provides a single, comprehensive Google OAuth integration supporting:
- Gmail (read, compose, modify)
- Google Calendar (events, scheduling)
- Google Drive (file access)

Replaces the previous fragmented gmail_config.py, google_auth.py, and google_setup_wizard.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mindroom.api.credentials import (
    RequestCredentialsTarget,
    consume_pending_oauth_request,
    issue_pending_oauth_state,
    load_credentials_for_target,
    resolve_request_credentials_target,
)
from mindroom.credentials import get_credentials_manager, save_scoped_credentials
from mindroom.tool_system.dependencies import ensure_tool_deps

if TYPE_CHECKING:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow

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

# Environment path for OAuth credentials
_ENV_PATH = Path(__file__).parent.parent.parent.parent.parent / ".env"

# Get configuration from environment
_GOOGLE_OAUTH_DEPS = ["google-auth", "google-auth-oauthlib"]


def _mindroom_port() -> str:
    return os.getenv("MINDROOM_PORT", "8765")


def _redirect_uri() -> str:
    return os.getenv("GOOGLE_REDIRECT_URI", f"http://localhost:{_mindroom_port()}/api/google/callback")


def _ensure_google_packages() -> tuple[type[GoogleRequest], type[Credentials], type[Flow]]:
    """Lazily import Google auth packages, auto-installing if needed."""
    ensure_tool_deps(_GOOGLE_OAUTH_DEPS, "gmail")

    from google.auth.transport.requests import Request as _GoogleRequest  # noqa: PLC0415
    from google.oauth2.credentials import Credentials as _Credentials  # noqa: PLC0415
    from google_auth_oauthlib.flow import Flow as _Flow  # noqa: PLC0415

    return _GoogleRequest, _Credentials, _Flow


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


def _get_oauth_credentials() -> dict[str, Any] | None:
    """Get OAuth credentials from environment variables."""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        return None

    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [_redirect_uri()],
        },
    }


def _build_google_token_data(creds: Credentials) -> dict[str, Any]:
    """Convert Google credentials to the stored token payload."""
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "_source": "ui",
    }

    id_token = creds.id_token
    if id_token:
        token_data["_id_token"] = id_token
    return token_data


def _get_google_credentials(target: RequestCredentialsTarget) -> Credentials | None:
    """Get Google credentials from stored token."""
    token_data = load_credentials_for_target("google", target)
    if not token_data:
        return None

    try:
        google_request_cls, credentials_cls, _ = _ensure_google_packages()
        creds = credentials_cls(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
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
        worker_scope=target.worker_scope,
        routing_agent_name=target.agent_name,
        credentials_manager=target.base_manager,
        execution_identity=target.execution_identity,
    )


def _save_env_credentials(client_id: str, client_secret: str, project_id: str | None = None) -> None:
    """Save OAuth credentials to .env file."""
    env_lines = []
    if _ENV_PATH.exists():
        with _ENV_PATH.open() as f:
            env_lines = f.readlines()

    # Update or add credentials
    # Use current environment variable for redirect URI to support multiple deployments
    current_redirect_uri = _redirect_uri()
    env_vars = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_PROJECT_ID": project_id or "mindroom-integration",
        "GOOGLE_REDIRECT_URI": current_redirect_uri,
        "MINDROOM_PORT": _mindroom_port(),
    }

    for key, value in env_vars.items():
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}="):
                env_lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            env_lines.append(f"{key}={value}\n")

    # Write back to .env file
    with _ENV_PATH.open("w") as f:
        f.writelines(env_lines)

    # Also set in current environment
    for key, value in env_vars.items():
        os.environ[key] = value


@router.get("/status")
async def get_status(request: Request, agent_name: str | None = None) -> GoogleStatus:
    """Check Google integration status."""
    # Check environment variables
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    has_credentials = bool(client_id and client_secret)

    # Get current credentials
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
    creds = _get_google_credentials(target)

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
    oauth_config = _get_oauth_credentials()
    if not oauth_config:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables.",
        )

    try:
        resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
        _, _, flow_cls = _ensure_google_packages()
        state = issue_pending_oauth_state(request, "google", agent_name)

        # Create OAuth flow with all scopes
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = _redirect_uri()
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

    oauth_config = _get_oauth_credentials()
    if not oauth_config:
        raise HTTPException(status_code=503, detail="OAuth not configured")

    try:
        _, _, flow_cls = _ensure_google_packages()

        # Create OAuth flow and exchange code for tokens
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = _redirect_uri()
        flow = flow_cls.from_client_config(oauth_config, scopes=_SCOPES, redirect_uri=current_redirect_uri)
        flow.fetch_token(code=code)

        # Save credentials
        target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
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
async def configure(credentials: dict[str, str]) -> dict[str, Any]:
    """Configure Google OAuth credentials manually."""
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    project_id = credentials.get("project_id", "mindroom-integration")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="client_id and client_secret are required",
        )

    try:
        # Save to environment
        _save_env_credentials(client_id, client_secret, project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save credentials: {e!s}") from e
    else:
        return {"success": True, "message": "Google OAuth credentials configured successfully"}


@router.post("/reset")
async def reset(request: Request) -> dict[str, Any]:
    """Reset Google integration by removing all credentials and tokens."""
    try:
        # Remove credentials using the manager
        runtime_paths = request.app.state.runtime_paths
        get_credentials_manager(storage_root=runtime_paths.storage_root).delete_credentials("google")

        # Remove from environment variables
        if _ENV_PATH.exists():
            with _ENV_PATH.open() as f:
                lines = f.readlines()

            # Filter out Google-related variables
            google_vars = [
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "GOOGLE_PROJECT_ID",
                "GOOGLE_REDIRECT_URI",
            ]
            filtered_lines = [line for line in lines if not any(line.startswith(f"{var}=") for var in google_vars)]

            with _ENV_PATH.open("w") as f:
                f.writelines(filtered_lines)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset: {e!s}") from e
    else:
        return {"success": True, "message": "Google integration reset successfully"}

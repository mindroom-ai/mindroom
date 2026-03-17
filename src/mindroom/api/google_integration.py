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

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.api.credentials import (
    RequestCredentialsTarget,
    consume_pending_oauth_request,
    issue_pending_oauth_state,
    load_credentials_for_target,
    resolve_request_credentials_target,
)
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.tool_system.dependencies import ensure_tool_deps
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow

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


def _mindroom_port(runtime_paths: RuntimePaths) -> str:
    return runtime_paths.env_value("MINDROOM_PORT", default="8765") or "8765"


def _redirect_uri(runtime_paths: RuntimePaths) -> str:
    default_redirect_uri = f"http://localhost:{_mindroom_port(runtime_paths)}/api/google/callback"
    return runtime_paths.env_value("GOOGLE_REDIRECT_URI", default=default_redirect_uri) or default_redirect_uri


def _ensure_google_packages(runtime_paths: RuntimePaths) -> tuple[type[GoogleRequest], type[Credentials], type[Flow]]:
    """Lazily import Google auth packages, auto-installing if needed."""
    ensure_tool_deps(_GOOGLE_OAUTH_DEPS, "gmail", runtime_paths)

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


def _get_oauth_credentials(runtime_paths: RuntimePaths) -> dict[str, Any] | None:
    """Get OAuth credentials from environment variables."""
    client_id = runtime_paths.env_value("GOOGLE_CLIENT_ID")
    client_secret = runtime_paths.env_value("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        return None

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


def _get_google_credentials(target: RequestCredentialsTarget, runtime_paths: RuntimePaths) -> Credentials | None:
    """Get Google credentials from stored token."""
    token_data = load_credentials_for_target("google", target)
    if not token_data:
        return None

    try:
        google_request_cls, credentials_cls, _ = _ensure_google_packages(runtime_paths)
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
        credentials_manager=target.base_manager,
        worker_target=resolve_worker_target(
            target.worker_scope,
            target.agent_name,
            execution_identity=target.execution_identity,
        ),
    )


def _refresh_runtime_paths(runtime_paths: RuntimePaths) -> RuntimePaths:
    """Reload one runtime context after mutating its sibling `.env` file."""
    return constants.resolve_runtime_paths(
        config_path=runtime_paths.config_path,
        storage_path=runtime_paths.storage_root,
        process_env=dict(runtime_paths.process_env),
    )


def _save_env_credentials(
    client_id: str,
    client_secret: str,
    runtime_paths: RuntimePaths,
    project_id: str | None = None,
) -> RuntimePaths:
    """Save OAuth credentials to .env file."""
    env_path = runtime_paths.env_path
    env_lines = []
    if env_path.exists():
        with env_path.open(encoding="utf-8") as f:
            env_lines = f.readlines()

    # Update or add credentials
    # Use current environment variable for redirect URI to support multiple deployments
    current_redirect_uri = _redirect_uri(runtime_paths)
    env_vars = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_PROJECT_ID": project_id or "mindroom-integration",
        "GOOGLE_REDIRECT_URI": current_redirect_uri,
        "MINDROOM_PORT": _mindroom_port(runtime_paths),
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
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with env_path.open("w", encoding="utf-8") as f:
        f.writelines(env_lines)

    return _refresh_runtime_paths(runtime_paths)


@router.get("/status")
async def get_status(request: Request, agent_name: str | None = None) -> GoogleStatus:
    """Check Google integration status."""
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    # Check environment variables
    runtime_paths = api_runtime_paths(request)
    client_id = runtime_paths.env_value("GOOGLE_CLIENT_ID")
    client_secret = runtime_paths.env_value("GOOGLE_CLIENT_SECRET")
    has_credentials = bool(client_id and client_secret)

    # Get current credentials
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
    creds = _get_google_credentials(target, runtime_paths)

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
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    runtime_paths = api_runtime_paths(request)
    oauth_config = _get_oauth_credentials(runtime_paths)
    if not oauth_config:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables.",
        )

    try:
        resolve_request_credentials_target(request, agent_name=agent_name, service_names=("google",))
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

    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    runtime_paths = api_runtime_paths(request)
    oauth_config = _get_oauth_credentials(runtime_paths)
    if not oauth_config:
        raise HTTPException(status_code=503, detail="OAuth not configured")

    try:
        _, _, flow_cls = _ensure_google_packages(runtime_paths)

        # Create OAuth flow and exchange code for tokens
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = _redirect_uri(runtime_paths)
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
async def configure(request: Request, credentials: dict[str, str]) -> dict[str, Any]:
    """Configure Google OAuth credentials manually."""
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    project_id = credentials.get("project_id", "mindroom-integration")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="client_id and client_secret are required",
        )

    config_reloaded = False
    try:
        # Save to environment
        runtime_paths = _save_env_credentials(
            client_id,
            client_secret,
            api_runtime_paths(request),
            project_id,
        )
        from mindroom.api.main import _load_config_from_file, initialize_api_app  # noqa: PLC0415

        config_lifecycle.load_runtime_config(runtime_paths)
        initialize_api_app(request.app, runtime_paths)
        config_reloaded = _load_config_from_file(runtime_paths, request.app)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save credentials: {e!s}") from e
    if not config_reloaded:
        raise HTTPException(status_code=500, detail="Failed to reload configuration after updating Google credentials.")
    return {"success": True, "message": "Google OAuth credentials configured successfully"}


@router.post("/reset")
async def reset(request: Request) -> dict[str, Any]:
    """Reset Google integration by removing all credentials and tokens."""
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    runtime_paths = api_runtime_paths(request)
    config_reloaded = False
    try:
        # Remove credentials using the manager
        get_runtime_credentials_manager(runtime_paths).delete_credentials("google")

        # Remove from environment variables
        env_path = runtime_paths.env_path
        if env_path.exists():
            with env_path.open(encoding="utf-8") as f:
                lines = f.readlines()

            # Filter out Google-related variables
            google_vars = [
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "GOOGLE_PROJECT_ID",
                "GOOGLE_REDIRECT_URI",
            ]
            filtered_lines = [line for line in lines if not any(line.startswith(f"{var}=") for var in google_vars)]

            with env_path.open("w", encoding="utf-8") as f:
                f.writelines(filtered_lines)
        refreshed_runtime_paths = _refresh_runtime_paths(runtime_paths)
        from mindroom.api.main import _load_config_from_file, initialize_api_app  # noqa: PLC0415

        config_lifecycle.load_runtime_config(refreshed_runtime_paths)
        initialize_api_app(request.app, refreshed_runtime_paths)
        config_reloaded = _load_config_from_file(refreshed_runtime_paths, request.app)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset: {e!s}") from e
    if not config_reloaded:
        raise HTTPException(
            status_code=500,
            detail="Failed to reload configuration after resetting Google integration.",
        )
    return {"success": True, "message": "Google integration reset successfully"}

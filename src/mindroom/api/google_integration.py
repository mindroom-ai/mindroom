"""Unified Google Integration for MindRoom.

This module provides a single, comprehensive Google OAuth integration supporting:
- Gmail (read, compose, modify)
- Google Calendar (events, scheduling)
- Google Drive (file access)

Replaces the previous fragmented gmail_config.py, google_auth.py, and google_setup_wizard.py
"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow  # type: ignore[import-untyped]
from pydantic import BaseModel

from mindroom.credentials import CredentialsManager

router = APIRouter(prefix="/api/google", tags=["google-integration"])

# Initialize credentials manager
creds_manager = CredentialsManager()

# OAuth scopes for all Google services needed by MindRoom
SCOPES = [
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
ENV_PATH = Path(__file__).parent.parent.parent.parent.parent / ".env"

# Get configuration from environment
BACKEND_PORT = os.getenv("BACKEND_PORT", "8765")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", f"http://localhost:{BACKEND_PORT}/api/google/callback")


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


def get_oauth_credentials() -> dict[str, Any] | None:
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
            "redirect_uris": [REDIRECT_URI],
        },
    }


def get_google_credentials() -> Credentials | None:
    """Get Google credentials from stored token."""
    token_data = creds_manager.load_credentials("google")
    if not token_data:
        return None

    try:
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes", SCOPES),
        )

        # Refresh token if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            # Save refreshed credentials
            save_credentials(creds)
    except Exception:
        return None
    else:
        return creds if creds and creds.valid else None


def save_credentials(creds: Credentials) -> None:
    """Save credentials using the unified credentials manager."""
    # Full token with all scopes
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    # Add ID token if available for user info
    if hasattr(creds, "_id_token") and creds._id_token:
        token_data["_id_token"] = creds._id_token

    # Save using credentials manager (handles backward compatibility)
    creds_manager.save_credentials("google", token_data)


def save_env_credentials(client_id: str, client_secret: str, project_id: str | None = None) -> None:
    """Save OAuth credentials to .env file."""
    env_lines = []
    if ENV_PATH.exists():
        with ENV_PATH.open() as f:
            env_lines = f.readlines()

    # Update or add credentials
    # Use current environment variable for redirect URI to support multiple deployments
    current_redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", REDIRECT_URI)
    env_vars = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_PROJECT_ID": project_id or "mindroom-integration",
        "GOOGLE_REDIRECT_URI": current_redirect_uri,
        "BACKEND_PORT": BACKEND_PORT,
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
    with ENV_PATH.open("w") as f:
        f.writelines(env_lines)

    # Also set in current environment
    for key, value in env_vars.items():
        os.environ[key] = value


@router.get("/status")
async def get_status() -> GoogleStatus:
    """Check Google integration status."""
    # Check environment variables
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    has_credentials = bool(client_id and client_secret)

    # Get current credentials
    creds = get_google_credentials()

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
            if hasattr(creds, "_id_token") and creds._id_token:
                decoded = jwt.decode(creds._id_token, options={"verify_signature": False})
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
async def connect() -> GoogleAuthUrl:
    """Start Google OAuth flow."""
    oauth_config = get_oauth_credentials()
    if not oauth_config:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables.",
        )

    try:
        # Create OAuth flow with all scopes
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", REDIRECT_URI)
        flow = Flow.from_client_config(oauth_config, scopes=SCOPES, redirect_uri=current_redirect_uri)

        # Generate authorization URL
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )

        return GoogleAuthUrl(auth_url=auth_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start Google OAuth: {e!s}") from e


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Google OAuth callback."""
    # Get the authorization code from the callback
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")

    oauth_config = get_oauth_credentials()
    if not oauth_config:
        raise HTTPException(status_code=503, detail="OAuth not configured")

    try:
        # Create OAuth flow and exchange code for tokens
        # Use current environment variable for redirect URI to support multiple deployments
        current_redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", REDIRECT_URI)
        flow = Flow.from_client_config(oauth_config, scopes=SCOPES, redirect_uri=current_redirect_uri)
        flow.fetch_token(code=code)

        # Save credentials
        save_credentials(flow.credentials)

        # Redirect back to widget with success message
        # Extract the domain from the redirect URI for the final redirect
        parsed_uri = urlparse(current_redirect_uri)
        base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
        return RedirectResponse(url=f"{base_url}/?google=connected")
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
async def disconnect() -> dict[str, str]:
    """Disconnect Google services by removing stored tokens."""
    try:
        # Remove credentials using the manager
        creds_manager.delete_credentials("google")
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
        save_env_credentials(client_id, client_secret, project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save credentials: {e!s}") from e
    else:
        return {"success": True, "message": "Google OAuth credentials configured successfully"}


@router.post("/reset")
async def reset() -> dict[str, Any]:
    """Reset Google integration by removing all credentials and tokens."""
    try:
        # Remove credentials using the manager
        creds_manager.delete_credentials("google")

        # Remove from environment variables
        if ENV_PATH.exists():
            with ENV_PATH.open() as f:
                lines = f.readlines()

            # Filter out Google-related variables
            google_vars = [
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "GOOGLE_PROJECT_ID",
                "GOOGLE_REDIRECT_URI",
            ]
            filtered_lines = [line for line in lines if not any(line.startswith(f"{var}=") for var in google_vars)]

            with ENV_PATH.open("w") as f:
                f.writelines(filtered_lines)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset: {e!s}") from e
    else:
        return {"success": True, "message": "Google integration reset successfully"}

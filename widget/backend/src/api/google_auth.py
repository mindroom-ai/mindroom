"""Simplified Google OAuth for Gmail and other Google services.

This module provides a user-friendly OAuth flow where users just click
"Login with Google" without needing to manage API keys.
"""

import json
import os
from pathlib import Path

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth/google", tags=["google-auth"])

# OAuth scopes for various Google services
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # Gmail access
    "https://www.googleapis.com/auth/calendar",  # Google Calendar
    "https://www.googleapis.com/auth/drive.file",  # Google Drive
    "openid",  # OpenID for user info
    "email",  # User email
    "profile",  # User profile
]

# Token storage path
TOKEN_PATH = Path(__file__).parent.parent.parent.parent.parent / "google_token.json"

# OAuth configuration from environment
# Get the backend port from environment, default to 8765
BACKEND_PORT = os.getenv("BACKEND_PORT", "8765")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", f"http://localhost:{BACKEND_PORT}/api/auth/google/callback")


class GoogleAuthStatus(BaseModel):
    """Google authentication status."""

    connected: bool
    email: str | None = None
    services: list[str] = []
    error: str | None = None


class GoogleAuthUrl(BaseModel):
    """Google OAuth URL response."""

    auth_url: str


# MindRoom's OAuth App Credentials
# In production, these would be stored securely and not in code
# This is the app that users authorize, so they don't need their own credentials
MINDROOM_OAUTH_CONFIG = {
    "web": {
        "client_id": "YOUR_MINDROOM_CLIENT_ID.apps.googleusercontent.com",
        "client_secret": "YOUR_MINDROOM_CLIENT_SECRET",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": [f"http://localhost:{BACKEND_PORT}/api/auth/google/callback"],
        "javascript_origins": ["http://localhost:5173", f"http://localhost:{BACKEND_PORT}"],
    },
}


def get_oauth_credentials() -> dict[str, str] | None:
    """Get OAuth credentials - uses MindRoom's app credentials.

    Users don't need to set up anything - they just authorize MindRoom's app
    to access their Google account.
    """
    # First check if custom credentials are provided (for development)
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if client_id and client_secret:
        # Developer mode - using custom credentials
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

    # Use MindRoom's OAuth app (would be properly configured in production)
    # For now, we'll check if placeholder values are still there
    if "YOUR_MINDROOM_CLIENT_ID" in MINDROOM_OAUTH_CONFIG["web"]["client_id"]:
        # Credentials not yet configured
        return None

    return MINDROOM_OAUTH_CONFIG


def get_google_credentials() -> Credentials | None:
    """Get Google credentials from stored token."""
    if not TOKEN_PATH.exists():
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        # Refresh token if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            # Save refreshed credentials
            with TOKEN_PATH.open("w") as token:
                token.write(creds.to_json())

        return creds if creds and creds.valid else None  # noqa: TRY300
    except Exception:
        return None


@router.get("/status")
async def get_google_status() -> GoogleAuthStatus:
    """Check Google services connection status."""
    creds = get_google_credentials()

    if not creds:
        return GoogleAuthStatus(connected=False)

    try:
        # Check which services are accessible based on scopes
        services = []
        if creds.has_scopes(["https://www.googleapis.com/auth/gmail.modify"]):
            services.append("Gmail")
        if creds.has_scopes(["https://www.googleapis.com/auth/calendar"]):
            services.append("Google Calendar")
        if creds.has_scopes(["https://www.googleapis.com/auth/drive.file"]):
            services.append("Google Drive")

        # Get user email from token

        try:
            # Decode the ID token to get user info
            if hasattr(creds, "_id_token") and creds._id_token:
                decoded = jwt.decode(creds._id_token, options={"verify_signature": False})
                email = decoded.get("email")
            else:
                email = None
        except Exception:
            email = None

        return GoogleAuthStatus(connected=True, email=email, services=services)
    except Exception as e:
        return GoogleAuthStatus(connected=False, error=str(e))


@router.post("/connect")
async def connect_google() -> GoogleAuthUrl:
    """Start Google OAuth flow with a simple 'Login with Google' experience."""
    # Check if credentials are available from environment
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        # Check for MindRoom default credentials
        oauth_config = get_oauth_credentials()
        if not oauth_config:
            # Provide helpful setup instructions for administrators
            raise HTTPException(
                status_code=503,
                detail="MindRoom's Google integration is not yet configured. "
                "An administrator needs to set up the OAuth app once for all users. "
                "See setup instructions in the documentation.",
            )
    else:
        oauth_config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": [REDIRECT_URI],
            },
        }

    try:
        # Create OAuth flow with all scopes
        # Use 'web' flow type for better user experience
        flow = Flow.from_client_config(oauth_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)

        # Generate authorization URL
        auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")

        return GoogleAuthUrl(auth_url=auth_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start Google login: {e!s}") from e


@router.get("/callback")
async def google_callback(request: Request) -> RedirectResponse:
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
        flow = Flow.from_client_config(oauth_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)

        flow.fetch_token(code=code)

        # Save credentials to token file
        creds = flow.credentials

        # Store the credentials
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

        with TOKEN_PATH.open("w") as token:
            json.dump(token_data, token, indent=2)

        # Redirect back to widget with success message
        return RedirectResponse(url="http://localhost:5173/?google=connected")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete Google login: {e!s}") from e


@router.post("/disconnect")
async def disconnect_google() -> dict[str, str]:
    """Disconnect Google services by removing stored token."""
    try:
        if TOKEN_PATH.exists():
            TOKEN_PATH.unlink()
        return {"status": "disconnected"}  # noqa: TRY300
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disconnect: {e!s}") from e

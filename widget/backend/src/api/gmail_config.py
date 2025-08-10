"""Gmail tool configuration for agents - handles both manual and OAuth setup."""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from google_auth_oauthlib.flow import Flow
from pydantic import BaseModel

router = APIRouter(prefix="/api/gmail", tags=["gmail-config"])

# Paths for credential storage
ENV_PATH = Path(__file__).parent.parent.parent.parent.parent / ".env"
TOKEN_PATH = Path(__file__).parent.parent.parent.parent.parent / "google_token.json"

# OAuth configuration
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "email",
    "profile",
]

# Get the backend port from environment, default to 8765
BACKEND_PORT = os.getenv("BACKEND_PORT", "8765")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", f"http://localhost:{BACKEND_PORT}/api/gmail/callback")


class GmailConfigRequest(BaseModel):
    """Request to configure Gmail."""

    client_id: str
    client_secret: str
    method: str = "manual"  # 'manual' or 'oauth'


class GmailStatus(BaseModel):
    """Gmail configuration status."""

    configured: bool
    method: str | None = None
    email: str | None = None
    hasCredentials: bool


@router.get("/status")
async def get_gmail_status():
    """Check if Gmail is configured for agents."""
    # Check environment variables
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    # Check if we have stored tokens
    has_token = TOKEN_PATH.exists()

    # Check if we have valid credentials (not test credentials)
    is_test_credentials = (
        client_id == "test-client.apps.googleusercontent.com"
        or client_secret == "test-secret-xyz"
        or client_secret == "test-secret"
        or client_secret == "test-secret-123"
    )

    configured = bool(client_id and client_secret and not is_test_credentials)

    email = None
    if has_token and configured:
        try:
            with open(TOKEN_PATH) as f:
                token_data = json.load(f)
                # Try to get email from token data
                if "_id_token" in token_data:
                    import jwt

                    try:
                        decoded = jwt.decode(token_data["_id_token"], options={"verify_signature": False})
                        email = decoded.get("email")
                    except Exception:
                        pass
        except Exception:
            pass

    return GmailStatus(
        configured=configured,
        method="oauth" if has_token else "manual" if configured else None,
        email=email,
        has_credentials=configured,
    )


@router.post("/configure")
async def configure_gmail(request: GmailConfigRequest):
    """Save Gmail credentials for agents to use."""
    # Load existing .env file
    env_lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            env_lines = f.readlines()

    # Update or add credentials
    env_vars = {
        "GOOGLE_CLIENT_ID": request.client_id,
        "GOOGLE_CLIENT_SECRET": request.client_secret,
        "GOOGLE_PROJECT_ID": "mindroom-integration",  # Default project ID
        "GOOGLE_REDIRECT_URI": REDIRECT_URI,
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
    with open(ENV_PATH, "w") as f:
        f.writelines(env_lines)

    # Also set in current environment
    for key, value in env_vars.items():
        os.environ[key] = value

    return {"success": True, "message": "Gmail credentials configured"}


@router.post("/oauth/start")
async def start_oauth_flow() -> dict[str, str | bool | None]:
    """Start OAuth flow for automatic setup."""
    # First try to use MindRoom's shared OAuth app (for better UX)
    client_id = os.getenv("MINDROOM_OAUTH_CLIENT_ID")
    client_secret = os.getenv("MINDROOM_OAUTH_CLIENT_SECRET")

    # Fall back to individual credentials if shared app not configured
    if not client_id or not client_secret:
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        # Return a response that prompts the user to set up credentials first
        return {
            "needs_credentials": True,
            "message": "OAuth is not configured. Please contact the administrator or set up your own Google OAuth credentials.",
            "auth_url": None,
        }

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
        flow = Flow.from_client_config(oauth_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
        auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")

        # Store flow in session or cache for callback
        # For simplicity, we'll recreate it in callback

        return {"auth_url": auth_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start OAuth: {e!s}") from e


@router.get("/callback")
async def oauth_callback(code: str) -> dict[str, str]:
    """Handle OAuth callback and save tokens."""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        # Try MindRoom defaults
        client_id = os.getenv("MINDROOM_DEFAULT_CLIENT_ID")
        client_secret = os.getenv("MINDROOM_DEFAULT_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="OAuth not configured")

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
        flow = Flow.from_client_config(oauth_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
        flow.fetch_token(code=code)

        # Save credentials
        creds = flow.credentials
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

        with TOKEN_PATH.open("w") as f:
            json.dump(token_data, f, indent=2)

        # Save credentials to .env as well
        await configure_gmail(GmailConfigRequest(client_id=client_id, client_secret=client_secret, method="oauth"))

        # Redirect back to widget with success
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="http://localhost:5173/?gmail=configured")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth callback failed: {e!s}") from e


@router.post("/reset")
async def reset_gmail_config() -> dict[str, str]:
    """Reset Gmail configuration."""
    # Remove token file
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()

    # Remove from environment variables
    if ENV_PATH.exists():
        with ENV_PATH.open() as f:
            lines = f.readlines()

        # Filter out Google-related variables
        filtered_lines = [
            line
            for line in lines
            if not any(
                line.startswith(f"{key}=")
                for key in ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_PROJECT_ID", "GOOGLE_REDIRECT_URI"]
            )
        ]

        with ENV_PATH.open("w") as f:
            f.writelines(filtered_lines)

    return {"success": True, "message": "Gmail configuration reset"}

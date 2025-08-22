"""Authentication module for MindRoom API."""

import os
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import APIRouter, HTTPException, Response, status
from fastapi.security import HTTPBasic
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["authentication"])

# Security
security = HTTPBasic(auto_error=False)

# Session storage (in production, use Redis or a database)
sessions: dict[str, dict] = {}

# Configuration from environment
AUTH_ENABLED = os.getenv("MINDROOM_AUTH_ENABLED", "false").lower() == "true"
AUTH_USERNAME = os.getenv("MINDROOM_AUTH_USERNAME", "admin")
AUTH_PASSWORD_HASH = os.getenv("MINDROOM_AUTH_PASSWORD_HASH", "")
SESSION_DURATION = int(os.getenv("MINDROOM_SESSION_DURATION", "86400"))  # 24 hours default


class LoginRequest(BaseModel):
    """Login request model."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Login response model."""

    success: bool
    message: str
    session_token: str | None = None
    expires_at: datetime | None = None


class AuthStatus(BaseModel):
    """Authentication status model."""

    enabled: bool
    authenticated: bool
    username: str | None = None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


def create_session(username: str) -> tuple[str, datetime]:
    """Create a new session token."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=SESSION_DURATION)

    sessions[token] = {
        "username": username,
        "expires_at": expires_at,
        "created_at": datetime.now(UTC),
    }

    return token, expires_at


def verify_session(token: str) -> str | None:
    """Verify a session token and return username if valid."""
    if not AUTH_ENABLED:
        return "anonymous"

    session = sessions.get(token)
    if not session:
        return None

    if datetime.now(UTC) > session["expires_at"]:
        # Session expired, remove it
        del sessions[token]
        return None

    return str(session["username"])


def get_current_user(session_token: str | None = None) -> str | None:
    """Get current user from session token (for dependency injection)."""
    if not AUTH_ENABLED:
        return "anonymous"

    if not session_token:
        return None

    return verify_session(session_token)


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, response: Response) -> LoginResponse:
    """Login endpoint."""
    if not AUTH_ENABLED:
        return LoginResponse(
            success=True,
            message="Authentication disabled",
            session_token="anonymous",
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )

    # Check if we have a password hash configured
    if not AUTH_PASSWORD_HASH:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication not properly configured",
        )

    # Verify credentials
    if request.username != AUTH_USERNAME:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    if not verify_password(request.password, AUTH_PASSWORD_HASH):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Create session
    token, expires_at = create_session(request.username)

    # Set cookie
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,  # Set to True in production with HTTPS
        samesite="strict",
        max_age=SESSION_DURATION,
    )

    return LoginResponse(
        success=True,
        message="Login successful",
        session_token=token,
        expires_at=expires_at,
    )


@router.post("/logout")
async def logout(response: Response, session_token: str | None = None) -> dict[str, bool | str]:
    """Logout endpoint."""
    if session_token and session_token in sessions:
        del sessions[session_token]

    response.delete_cookie(key="session_token")

    return {"success": True, "message": "Logged out successfully"}


@router.get("/status", response_model=AuthStatus)
async def auth_status(session_token: str | None = None) -> AuthStatus:
    """Check authentication status."""
    if not AUTH_ENABLED:
        return AuthStatus(
            enabled=False,
            authenticated=True,
            username="anonymous",
        )

    username = verify_session(session_token) if session_token else None

    return AuthStatus(
        enabled=True,
        authenticated=username is not None,
        username=username,
    )


@router.post("/check")
async def check_auth(session_token: str | None = None) -> dict[str, bool | str]:
    """Check if current session is valid."""
    if not AUTH_ENABLED:
        return {"valid": True, "username": "anonymous"}

    username = verify_session(session_token) if session_token else None

    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )

    return {"valid": True, "username": username}


def hash_password(password: str) -> str:
    """Hash a password for storing (utility function)."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

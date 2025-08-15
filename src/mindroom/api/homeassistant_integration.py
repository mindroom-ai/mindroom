"""Home Assistant Integration for MindRoom.

This module provides OAuth2 integration with Home Assistant, supporting:
- Device control (lights, switches, climate, etc.)
- State monitoring (sensors, binary sensors)
- Scene activation
- Service calls
- Automation triggers

Uses the official Home Assistant REST API.
"""

import os
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mindroom.credentials import CredentialsManager

router = APIRouter(prefix="/api/homeassistant", tags=["homeassistant-integration"])

# Initialize credentials manager
creds_manager = CredentialsManager()

# OAuth scopes for Home Assistant
# Home Assistant doesn't use traditional OAuth scopes, but we request full API access
SCOPES: list[str] = []

# Get configuration from environment
BACKEND_PORT = os.getenv("BACKEND_PORT", "8765")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")


class HomeAssistantStatus(BaseModel):
    """Home Assistant integration status."""

    connected: bool
    instance_url: str | None = None
    version: str | None = None
    location_name: str | None = None
    error: str | None = None
    has_credentials: bool = False
    entities_count: int = 0


class HomeAssistantAuthUrl(BaseModel):
    """Home Assistant OAuth URL response."""

    auth_url: str


class HomeAssistantConfig(BaseModel):
    """Home Assistant configuration."""

    instance_url: str
    client_id: str | None = None
    long_lived_token: str | None = None


def get_stored_config() -> dict[str, Any] | None:
    """Get stored Home Assistant configuration."""
    return creds_manager.load_credentials("homeassistant")


def save_config(config: dict[str, Any]) -> None:
    """Save Home Assistant configuration."""
    creds_manager.save_credentials("homeassistant", config)


async def test_connection(instance_url: str, token: str) -> dict[str, Any]:
    """Test connection to Home Assistant."""
    async with httpx.AsyncClient() as client:
        try:
            # Test API connection
            response = await client.get(
                urljoin(instance_url, "/api/"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )

            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to connect to Home Assistant: {response.text}",
                )

            api_info = response.json()

            # Get config for more details
            config_response = await client.get(
                urljoin(instance_url, "/api/config"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )

            config_info = config_response.json() if config_response.status_code == 200 else {}

            # Get states to count entities
            states_response = await client.get(
                urljoin(instance_url, "/api/states"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )

            entities = states_response.json() if states_response.status_code == 200 else []

            return {
                "message": api_info.get("message", "API running"),
                "version": config_info.get("version", "unknown"),
                "location_name": config_info.get("location_name", "Home"),
                "entities_count": len(entities),
            }

        except httpx.TimeoutException as e:
            raise HTTPException(
                status_code=504,
                detail="Connection timeout - check if the URL is correct and accessible",
            ) from e
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Connection error: {e!s}",
            ) from e


@router.get("/status")
async def get_status() -> HomeAssistantStatus:
    """Check Home Assistant integration status."""
    config = get_stored_config()

    if not config:
        return HomeAssistantStatus(
            connected=False,
            has_credentials=False,
        )

    try:
        # Test the connection
        instance_url = config.get("instance_url")
        token = config.get("access_token") or config.get("long_lived_token")

        if not instance_url or not token:
            return HomeAssistantStatus(
                connected=False,
                has_credentials=True,
                error="Missing instance URL or token",
            )

        info = await test_connection(instance_url, token)

        return HomeAssistantStatus(
            connected=True,
            instance_url=instance_url,
            version=info.get("version"),
            location_name=info.get("location_name"),
            has_credentials=True,
            entities_count=info.get("entities_count", 0),
        )

    except HTTPException as e:
        return HomeAssistantStatus(
            connected=False,
            has_credentials=True,
            error=e.detail,
        )
    except Exception as e:
        return HomeAssistantStatus(
            connected=False,
            has_credentials=True,
            error=str(e),
        )


@router.post("/connect/oauth")
async def connect_oauth(config: HomeAssistantConfig) -> HomeAssistantAuthUrl:
    """Start Home Assistant OAuth flow."""
    if not config.instance_url:
        raise HTTPException(
            status_code=400,
            detail="Home Assistant instance URL is required",
        )

    if not config.client_id:
        raise HTTPException(
            status_code=400,
            detail="OAuth Client ID is required for OAuth flow",
        )

    # Build OAuth authorization URL
    # Home Assistant OAuth2 flow: https://developers.home-assistant.io/docs/auth_api/
    redirect_uri = f"{FRONTEND_URL}/homeassistant-callback"

    auth_params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }

    # Build query string
    query_string = "&".join(f"{k}={v}" for k, v in auth_params.items())
    auth_url = f"{config.instance_url}/auth/authorize?{query_string}"

    # Store config for callback
    temp_config = {
        "instance_url": config.instance_url,
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
    }
    save_config(temp_config)

    return HomeAssistantAuthUrl(auth_url=auth_url)


@router.post("/connect/token")
async def connect_token(config: HomeAssistantConfig) -> dict[str, str]:
    """Connect using a long-lived access token."""
    if not config.instance_url:
        raise HTTPException(
            status_code=400,
            detail="Home Assistant instance URL is required",
        )

    if not config.long_lived_token:
        raise HTTPException(
            status_code=400,
            detail="Long-lived access token is required",
        )

    # Normalize the instance URL
    instance_url = config.instance_url.rstrip("/")
    if not instance_url.startswith(("http://", "https://")):
        instance_url = f"http://{instance_url}"

    # Test the connection
    try:
        await test_connection(instance_url, config.long_lived_token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {e!s}",
        ) from e

    # Save configuration
    save_config(
        {
            "instance_url": instance_url,
            "long_lived_token": config.long_lived_token,
        },
    )

    return {"status": "connected", "message": "Successfully connected to Home Assistant"}


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Home Assistant OAuth callback."""
    # Get the authorization code from the callback
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")

    # Get stored config
    config = get_stored_config()
    if not config:
        raise HTTPException(status_code=503, detail="No configuration found")

    instance_url = config.get("instance_url")
    client_id = config.get("client_id")

    if not all([instance_url, client_id]) or not isinstance(instance_url, str):
        raise HTTPException(status_code=503, detail="Incomplete configuration")

    try:
        # Exchange code for access token
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                urljoin(instance_url, "/auth/token"),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                },
                timeout=10.0,
            )

            if token_response.status_code != 200:
                raise HTTPException(
                    status_code=token_response.status_code,
                    detail=f"Failed to get access token: {token_response.text}",
                )

            token_data = token_response.json()

            # Save the access token
            save_config(
                {
                    "instance_url": instance_url,
                    "client_id": client_id,
                    "access_token": token_data.get("access_token"),
                    "refresh_token": token_data.get("refresh_token"),
                    "expires_in": token_data.get("expires_in"),
                },
            )

            # Redirect back to widget with success message
            return RedirectResponse(url=f"{FRONTEND_URL}/?homeassistant=connected")

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Failed to exchange code: {e!s}") from e


@router.post("/disconnect")
async def disconnect() -> dict[str, str]:
    """Disconnect Home Assistant by removing stored tokens."""
    try:
        # Remove credentials using the manager
        creds_manager.delete_credentials("homeassistant")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disconnect: {e!s}") from e
    else:
        return {"status": "disconnected"}


@router.get("/entities")
async def get_entities(domain: str | None = None) -> list[dict[str, Any]]:
    """Get Home Assistant entities."""
    config = get_stored_config()
    if not config:
        raise HTTPException(status_code=401, detail="Not connected to Home Assistant")

    instance_url = config.get("instance_url")
    token = config.get("access_token") or config.get("long_lived_token")

    if not instance_url or not token:
        raise HTTPException(status_code=401, detail="Missing credentials")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                urljoin(instance_url, "/api/states"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to get entities: {response.text}",
                )

            entities = response.json()

            # Filter by domain if specified
            if domain:
                entities = [e for e in entities if e["entity_id"].startswith(f"{domain}.")]

            # Simplify the response
            return [
                {
                    "entity_id": e["entity_id"],
                    "state": e["state"],
                    "attributes": e.get("attributes", {}),
                    "last_changed": e.get("last_changed"),
                }
                for e in entities
            ]

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Failed to get entities: {e!s}") from e


@router.post("/service")
async def call_service(
    domain: str,
    service: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call a Home Assistant service."""
    config = get_stored_config()
    if not config:
        raise HTTPException(status_code=401, detail="Not connected to Home Assistant")

    instance_url = config.get("instance_url")
    token = config.get("access_token") or config.get("long_lived_token")

    if not instance_url or not token:
        raise HTTPException(status_code=401, detail="Missing credentials")

    # Build service data
    service_data = data or {}
    if entity_id:
        service_data["entity_id"] = entity_id

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                urljoin(instance_url, f"/api/services/{domain}/{service}"),
                headers={"Authorization": f"Bearer {token}"},
                json=service_data,
                timeout=10.0,
            )

            if response.status_code not in (200, 201):
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to call service: {response.text}",
                )

            return {"success": True, "message": f"Service {domain}.{service} called successfully"}

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Failed to call service: {e!s}") from e

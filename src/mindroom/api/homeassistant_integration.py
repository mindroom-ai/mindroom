"""Dashboard routes that connect, check, and disconnect a Home Assistant instance."""

from __future__ import annotations

from functools import partial
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mindroom.api.auth import browser_origin
from mindroom.api.credentials_oauth_flows import consume_pending_oauth_request, issue_pending_oauth_state
from mindroom.api.credentials_target import (
    RequestCredentialsTarget,
    delete_credentials_for_target,
    load_credentials_for_target,
    resolve_request_credentials_target,
    save_credentials_for_target,
)
from mindroom.homeassistant_requests import send_homeassistant_request
from mindroom.homeassistant_url_validation import homeassistant_url_error_detail, validate_homeassistant_instance_url
from mindroom.server_fetch_url import ServerFetchAsyncHTTPTransport, ServerFetchUrlError

router = APIRouter(prefix="/api/homeassistant", tags=["homeassistant-integration"])


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
    long_lived_token: str | None = None
    allow_private_url: bool = False


def _normalize_instance_url(instance_url: str) -> str:
    """Normalize a Home Assistant instance URL."""
    normalized = instance_url.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"
    return normalized


def _save_config(target: RequestCredentialsTarget, config: dict[str, Any]) -> None:
    """Save Home Assistant configuration."""
    config_to_save = dict(config)
    instance_url = config_to_save.get("instance_url")
    if isinstance(instance_url, str):
        config_to_save["instance_url"] = _normalize_instance_url(instance_url)
    config_to_save.setdefault("_source", "ui")
    save_credentials_for_target("homeassistant", config_to_save, target)


def _validate_homeassistant_instance_url(instance_url: str, *, allow_private_url: bool) -> str:
    """Validate a Home Assistant instance URL before server-side fetches."""
    try:
        return validate_homeassistant_instance_url(instance_url, allow_private_url=allow_private_url)
    except ServerFetchUrlError as e:
        raise _homeassistant_url_http_error(e, allow_private_url=allow_private_url) from e


def _homeassistant_url_http_error(error: ServerFetchUrlError, *, allow_private_url: bool) -> HTTPException:
    """Build the API error returned for rejected Home Assistant URLs."""
    return HTTPException(
        status_code=400,
        detail=homeassistant_url_error_detail(error, allow_private_url=allow_private_url),
    )


def _homeassistant_http_client(*, allow_private_url: bool) -> httpx.AsyncClient:
    """Return an async HTTP client that validates the address it connects to."""
    return httpx.AsyncClient(
        transport=ServerFetchAsyncHTTPTransport(allow_private_networks=allow_private_url),
    )


async def _test_connection(config: dict[str, Any], target: RequestCredentialsTarget) -> dict[str, Any]:
    """Test connection to Home Assistant, renewing an expired OAuth access token."""
    allow_private_url = config.get("allow_private_url") is True
    try:
        instance_url = _normalize_instance_url(config["instance_url"])
        fetch_url = _validate_homeassistant_instance_url(instance_url, allow_private_url=allow_private_url)
        async with _homeassistant_http_client(allow_private_url=allow_private_url) as client:

            async def get(endpoint: str) -> httpx.Response:
                return await send_homeassistant_request(
                    client,
                    fetch_url,
                    config,
                    "GET",
                    endpoint,
                    save_config=partial(_save_config, target),
                )

            # Test API connection
            response = await get("/api/")

            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Invalid authentication token")
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to connect to Home Assistant: HTTP {response.status_code}",
                )

            api_info = response.json()

            # Get config for more details
            config_response = await get("/api/config")

            config_info = config_response.json() if config_response.status_code == 200 else {}

            # Get states to count entities
            states_response = await get("/api/states")

            entities = states_response.json() if states_response.status_code == 200 else []

            return {
                "message": api_info.get("message", "API running"),
                "version": config_info.get("version", "unknown"),
                "location_name": config_info.get("location_name", "Home"),
                "entities_count": len(entities),
            }

    except ServerFetchUrlError as e:
        raise _homeassistant_url_http_error(e, allow_private_url=allow_private_url) from e
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
async def get_status(request: Request, agent_name: str | None = None) -> HomeAssistantStatus:
    """Check Home Assistant integration status."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("homeassistant",))
    config = load_credentials_for_target("homeassistant", target)

    if not config:
        return HomeAssistantStatus(
            connected=False,
            has_credentials=False,
        )

    instance_url = config.get("instance_url")
    token = config.get("access_token") or config.get("long_lived_token")
    if not instance_url or not token:
        return HomeAssistantStatus(
            connected=False,
            has_credentials=True,
            error="Missing instance URL or token",
        )

    try:
        info = await _test_connection(config, target)

        return HomeAssistantStatus(
            connected=True,
            instance_url=_normalize_instance_url(instance_url),
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
async def connect_oauth(
    request: Request,
    config: HomeAssistantConfig,
    agent_name: str | None = None,
) -> HomeAssistantAuthUrl:
    """Start Home Assistant OAuth flow."""
    if not config.instance_url:
        raise HTTPException(
            status_code=400,
            detail="Home Assistant instance URL is required",
        )

    instance_url = _normalize_instance_url(config.instance_url)

    # Build OAuth authorization URL
    # Home Assistant OAuth2 flow: https://developers.home-assistant.io/docs/auth_api/
    resolve_request_credentials_target(request, agent_name=agent_name, service_names=("homeassistant",))
    fetch_url = _validate_homeassistant_instance_url(instance_url, allow_private_url=config.allow_private_url)
    # Home Assistant identifies a client by a URL whose scheme and host the redirect URI must share: MindRoom's origin.
    client_id = browser_origin(request)
    if client_id is None:
        raise HTTPException(status_code=503, detail="MINDROOM_PUBLIC_URL is not a valid URL")
    state = issue_pending_oauth_state(
        request,
        "homeassistant",
        agent_name,
        payload={
            "instance_url": instance_url,
            "client_id": client_id,
            "allow_private_url": "true" if config.allow_private_url else "false",
        },
    )

    auth_params = {
        "client_id": client_id,
        "redirect_uri": f"{client_id}/api/homeassistant/callback",
        "response_type": "code",
        "state": state,
    }

    # Build query string
    auth_url = f"{fetch_url}/auth/authorize?{urlencode(auth_params)}"

    return HomeAssistantAuthUrl(auth_url=auth_url)


@router.post("/connect/token")
async def connect_token(
    request: Request,
    config: HomeAssistantConfig,
    agent_name: str | None = None,
) -> dict[str, str]:
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
    instance_url = _normalize_instance_url(config.instance_url)
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=("homeassistant",),
    )
    _validate_homeassistant_instance_url(instance_url, allow_private_url=config.allow_private_url)
    connection = {
        "instance_url": instance_url,
        "long_lived_token": config.long_lived_token,
        "allow_private_url": config.allow_private_url,
    }

    # Test the connection
    try:
        await _test_connection(connection, target)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {e!s}",
        ) from e

    _save_config(target, connection)

    return {"status": "connected", "message": "Successfully connected to Home Assistant"}


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Home Assistant OAuth callback."""
    # Get the authorization code from the callback
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")

    state = request.query_params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="No OAuth state received")

    pending = consume_pending_oauth_request(request, "homeassistant", state)
    agent_name = pending.agent_name
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=("homeassistant",),
        execution_scope_override_provided=pending.execution_scope_override_provided,
        execution_scope_override=pending.execution_scope_override,
    )

    payload = pending.payload or {}
    instance_url = payload.get("instance_url")
    client_id = payload.get("client_id")
    allow_private_url = payload.get("allow_private_url") == "true"

    if not all([instance_url, client_id]) or not isinstance(instance_url, str):
        raise HTTPException(status_code=503, detail="Incomplete configuration")
    instance_url = _normalize_instance_url(instance_url)
    fetch_url = _validate_homeassistant_instance_url(instance_url, allow_private_url=allow_private_url)

    try:
        # Exchange code for access token
        async with _homeassistant_http_client(allow_private_url=allow_private_url) as client:
            token_response = await client.post(
                urljoin(fetch_url, "/auth/token"),
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
                    detail=f"Failed to get access token: HTTP {token_response.status_code}",
                )

            token_data = token_response.json()

            # Save the access token
            _save_config(
                target,
                {
                    "instance_url": instance_url,
                    "client_id": client_id,
                    "access_token": token_data.get("access_token"),
                    "refresh_token": token_data.get("refresh_token"),
                    "expires_in": token_data.get("expires_in"),
                    "allow_private_url": allow_private_url,
                },
            )

            return RedirectResponse(url="/?homeassistant=connected")

    except ServerFetchUrlError as e:
        raise _homeassistant_url_http_error(e, allow_private_url=allow_private_url) from e
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Failed to exchange code: {e!s}") from e


@router.post("/disconnect")
async def disconnect(request: Request, agent_name: str | None = None) -> dict[str, str]:
    """Disconnect Home Assistant by removing stored tokens."""
    try:
        target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("homeassistant",))
        delete_credentials_for_target("homeassistant", target)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disconnect: {e!s}") from e
    else:
        return {"status": "disconnected"}

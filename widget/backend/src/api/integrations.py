"""Third-party service integrations API."""

import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from spotipy import Spotify, SpotifyOAuth

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Base path for storing credentials
CREDS_PATH = Path(__file__).parent.parent.parent.parent.parent


# Load tool metadata from the single source of truth
def get_tools_metadata() -> dict[str, Any]:
    """Load tool metadata from JSON file."""
    json_path = Path(__file__).parent.parent.parent.parent.parent / "mindroom/tools_metadata.json"
    if not json_path.exists():
        return {}

    with json_path.open() as f:
        data = json.load(f)
        # Convert to dict keyed by tool name for easy lookup
        return {tool["name"]: tool for tool in data.get("tools", [])}


class ServiceStatus(BaseModel):
    """Service connection status."""

    service: str
    connected: bool
    display_name: str
    icon: str
    category: str
    requires_oauth: bool
    requires_api_key: bool
    details: dict[str, Any] | None = None
    error: str | None = None


class ApiKeyRequest(BaseModel):
    """API key configuration request."""

    service: str
    api_key: str
    api_secret: str | None = None


def get_service_credentials(service: str) -> dict[str, Any]:
    """Get stored credentials for a service."""
    creds_file = CREDS_PATH / f"{service}_credentials.json"
    if not creds_file.exists():
        return {}

    try:
        with creds_file.open() as f:
            return json.load(f)
    except Exception:
        return {}


def save_service_credentials(service: str, credentials: dict[str, Any]) -> None:
    """Save service credentials."""
    creds_file = CREDS_PATH / f"{service}_credentials.json"
    with creds_file.open("w") as f:
        json.dump(credentials, f, indent=2)


@router.get("/status")
async def get_all_services_status() -> list[ServiceStatus]:
    """Get connection status for all services."""
    statuses = []

    # Get tool metadata from single source of truth
    tools_metadata = get_tools_metadata()

    # Filter to only services that support OAuth or API key
    service_tools = {
        name: tool for name, tool in tools_metadata.items() if tool.get("setup_type") in ["oauth", "api_key", "special"]
    }

    for service_id, tool in service_tools.items():
        status = ServiceStatus(
            service=service_id,
            connected=False,
            display_name=tool.get("display_name", service_id),
            icon=tool.get("icon", "📦"),
            category=tool.get("category", "other"),
            requires_oauth=tool.get("setup_type") == "oauth",
            requires_api_key=tool.get("setup_type") == "api_key",
        )

        # Check if service is configured
        creds = get_service_credentials(service_id)
        if creds:
            if service_id == "spotify":
                # Check Spotify OAuth token
                status.connected = "access_token" in creds
                if status.connected:
                    status.details = {"username": creds.get("username")}
            else:
                # Check API key services
                status.connected = "api_key" in creds
                if status.connected and service_id == "amazon":
                    status.details = {"has_access_key": bool(creds.get("access_key"))}

        statuses.append(status)

    return statuses


@router.get("/{service}/status")
async def get_service_status(service: str) -> ServiceStatus:
    """Get connection status for a specific service."""
    # Get tool metadata from single source of truth
    tools_metadata = get_tools_metadata()

    if service not in tools_metadata:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")

    tool = tools_metadata[service]
    status = ServiceStatus(
        service=service,
        connected=False,
        display_name=tool.get("display_name", service),
        icon=tool.get("icon", "📦"),
        category=tool.get("category", "other"),
        requires_oauth=tool.get("setup_type") == "oauth",
        requires_api_key=tool.get("setup_type") == "api_key",
    )

    creds = get_service_credentials(service)
    if creds:
        if service == "spotify":
            status.connected = "access_token" in creds
            if status.connected:
                try:
                    # Try to get user info
                    sp = Spotify(auth=creds["access_token"])
                    user = sp.current_user()
                    status.details = {
                        "username": user["display_name"],
                        "email": user.get("email"),
                        "product": user.get("product"),
                    }
                except Exception as e:
                    status.connected = False
                    status.error = str(e)
        else:
            status.connected = "api_key" in creds

    return status


# Amazon Shopping
@router.post("/amazon/configure")
async def configure_amazon(request: ApiKeyRequest) -> dict[str, str]:
    """Configure Amazon Product Advertising API credentials."""
    if request.service != "amazon":
        raise HTTPException(status_code=400, detail="Invalid service")

    credentials = {
        "api_key": request.api_key,
        "access_key": request.api_secret,
        "partner_tag": os.getenv("AMAZON_PARTNER_TAG", "mindroom-20"),
        "region": os.getenv("AMAZON_REGION", "US"),
    }

    save_service_credentials("amazon", credentials)
    return {"status": "configured"}


@router.post("/amazon/search")
async def search_amazon(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search Amazon products."""
    creds = get_service_credentials("amazon")
    if not creds or "api_key" not in creds:
        raise HTTPException(status_code=401, detail="Amazon not configured")

    # Note: Amazon Product Advertising API requires signing requests
    # This is a simplified example - in production, use proper request signing
    try:
        # For demo purposes, return mock data
        # In production, implement proper Amazon API integration
        products = [
            {
                "title": f"Product {i + 1} matching '{query}'",
                "price": f"${19.99 + i * 10}",
                "rating": 4.5 - i * 0.1,
                "url": f"https://amazon.com/dp/B00{i}EXAMPLE",
            }
            for i in range(min(max_results, 5))
        ]
        return {"query": query, "results": products}  # noqa: TRY300
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}") from e


# IMDb
@router.post("/imdb/configure")
async def configure_imdb(request: ApiKeyRequest) -> dict[str, str]:
    """Configure IMDb/OMDB API key."""
    if request.service != "imdb":
        raise HTTPException(status_code=400, detail="Invalid service")

    # Test the API key
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://www.omdbapi.com/?apikey={request.api_key}&t=Inception",
                timeout=10,  # Add timeout for security
            )
        data = response.json()
        if data.get("Response") == "False":
            raise HTTPException(status_code=400, detail="Invalid API key")
    except httpx.RequestError as e:
        raise HTTPException(status_code=400, detail=f"Failed to validate API key: {e!s}") from e

    credentials = {"api_key": request.api_key}
    save_service_credentials("imdb", credentials)
    return {"status": "configured"}


@router.get("/imdb/search")
async def search_imdb(query: str, type: str = "movie") -> dict[str, Any]:  # noqa: A002
    """Search IMDb for movies/shows."""
    creds = get_service_credentials("imdb")
    if not creds or "api_key" not in creds:
        raise HTTPException(status_code=401, detail="IMDb not configured")

    try:
        # Use OMDB API for IMDb data
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "http://www.omdbapi.com/",
                params={
                    "apikey": creds["api_key"],
                    "s": query,
                    "type": type,
                },
                timeout=10,  # Add timeout
            )
        data = response.json()

        if data.get("Response") == "False":
            return {"query": query, "results": [], "error": data.get("Error")}

        results = [
            {
                "title": item["Title"],
                "year": item["Year"],
                "type": item["Type"],
                "imdb_id": item["imdbID"],
                "poster": item.get("Poster"),
            }
            for item in data.get("Search", [])
        ]

        return {"query": query, "results": results}  # noqa: TRY300
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}") from e


@router.get("/imdb/details/{imdb_id}")
async def get_imdb_details(imdb_id: str) -> dict[str, Any]:
    """Get detailed information about a movie/show."""
    creds = get_service_credentials("imdb")
    if not creds or "api_key" not in creds:
        raise HTTPException(status_code=401, detail="IMDb not configured")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "http://www.omdbapi.com/",
                params={
                    "apikey": creds["api_key"],
                    "i": imdb_id,
                    "plot": "full",
                },
                timeout=10,  # Add timeout
            )
        data = response.json()

        if data.get("Response") == "False":
            raise HTTPException(status_code=404, detail=data.get("Error"))  # noqa: TRY301

        return data  # noqa: TRY300
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get details: {e!s}") from e


# Spotify
@router.post("/spotify/connect")
async def connect_spotify() -> dict[str, str]:
    """Start Spotify OAuth flow."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="Spotify OAuth not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.",
        )

    sp_oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost:8000/api/integrations/spotify/callback",
        scope="user-read-private user-read-email user-read-playback-state user-read-currently-playing user-top-read",
    )

    auth_url = sp_oauth.get_authorize_url()
    return {"auth_url": auth_url}


@router.get("/spotify/callback")
async def spotify_callback(code: str) -> RedirectResponse:
    """Handle Spotify OAuth callback."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Spotify OAuth not configured")

    try:
        sp_oauth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri="http://localhost:8000/api/integrations/spotify/callback",
        )

        token_info = sp_oauth.get_access_token(code)

        # Get user info
        sp = Spotify(auth=token_info["access_token"])
        user = sp.current_user()

        # Save credentials
        credentials = {
            "access_token": token_info["access_token"],
            "refresh_token": token_info.get("refresh_token"),
            "expires_at": token_info.get("expires_at"),
            "username": user["display_name"],
        }
        save_service_credentials("spotify", credentials)

        # Redirect back to widget
        return RedirectResponse(url="http://localhost:5173/?spotify=connected")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth failed: {e!s}") from e


@router.get("/spotify/current")
async def get_spotify_current() -> dict[str, Any]:
    """Get currently playing track on Spotify."""
    creds = get_service_credentials("spotify")
    if not creds or "access_token" not in creds:
        raise HTTPException(status_code=401, detail="Spotify not connected")

    try:
        sp = Spotify(auth=creds["access_token"])
        current = sp.current_playback()

        if not current or not current.get("item"):
            return {"playing": False}

        track = current["item"]
        return {
            "playing": current["is_playing"],
            "track": track["name"],
            "artist": ", ".join([a["name"] for a in track["artists"]]),
            "album": track["album"]["name"],
            "progress_ms": current["progress_ms"],
            "duration_ms": track["duration_ms"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get current track: {e!s}") from e


@router.get("/spotify/top-tracks")
async def get_spotify_top_tracks(limit: int = 10) -> dict[str, Any]:
    """Get user's top tracks."""
    creds = get_service_credentials("spotify")
    if not creds or "access_token" not in creds:
        raise HTTPException(status_code=401, detail="Spotify not connected")

    try:
        sp = Spotify(auth=creds["access_token"])
        results = sp.current_user_top_tracks(limit=limit)

        tracks = [
            {
                "name": track["name"],
                "artist": ", ".join([a["name"] for a in track["artists"]]),
                "album": track["album"]["name"],
                "popularity": track["popularity"],
            }
            for track in results["items"]
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get top tracks: {e!s}") from e
    else:
        return {"tracks": tracks}


# Walmart
@router.post("/walmart/configure")
async def configure_walmart(request: ApiKeyRequest) -> dict[str, str]:
    """Configure Walmart API credentials."""
    if request.service != "walmart":
        raise HTTPException(status_code=400, detail="Invalid service")

    credentials = {"api_key": request.api_key}
    save_service_credentials("walmart", credentials)
    return {"status": "configured"}


@router.get("/walmart/search")
async def search_walmart(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search Walmart products."""
    creds = get_service_credentials("walmart")
    if not creds or "api_key" not in creds:
        raise HTTPException(status_code=401, detail="Walmart not configured")

    try:
        # Note: This is a simplified example
        # In production, use the actual Walmart Open API

        # For demo purposes, return mock data
        products = [
            {
                "name": f"Product {i + 1} - {query}",
                "price": f"${9.99 + i * 5}",
                "in_stock": i % 2 == 0,
                "url": f"https://walmart.com/ip/{i}",
            }
            for i in range(min(max_results, 5))
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}") from e
    else:
        return {"query": query, "results": products}


@router.post("/{service}/disconnect")
async def disconnect_service(service: str) -> dict[str, str]:
    """Disconnect a service by removing stored credentials."""
    # Get tool metadata from single source of truth
    tools_metadata = get_tools_metadata()

    if service not in tools_metadata:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")

    creds_file = CREDS_PATH / f"{service}_credentials.json"
    if creds_file.exists():
        creds_file.unlink()

    return {"status": "disconnected"}

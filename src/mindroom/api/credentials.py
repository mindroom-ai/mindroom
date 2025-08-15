"""Unified credentials management API."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mindroom.credentials import get_credentials_manager

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


class SetApiKeyRequest(BaseModel):
    """Request to set an API key."""

    service: str
    api_key: str
    key_name: str = "api_key"


class CredentialStatus(BaseModel):
    """Status of a service's credentials."""

    service: str
    has_credentials: bool
    key_names: list[str] | None = None


class SetCredentialsRequest(BaseModel):
    """Request to set multiple credentials for a service."""

    credentials: dict[str, Any]  # Can be strings, booleans, numbers, etc.


@router.get("/list")
async def list_services() -> list[str]:
    """List all services with stored credentials."""
    manager = get_credentials_manager()
    return manager.list_services()


@router.get("/{service}/status")
async def get_credential_status(service: str) -> CredentialStatus:
    """Get the status of credentials for a service."""
    manager = get_credentials_manager()
    credentials = manager.load_credentials(service)

    if credentials:
        return CredentialStatus(
            service=service,
            has_credentials=True,
            key_names=list(credentials.keys()) if isinstance(credentials, dict) else None,
        )

    return CredentialStatus(service=service, has_credentials=False)


@router.post("/{service}")
async def set_credentials(service: str, request: SetCredentialsRequest) -> dict[str, str]:
    """Set multiple credentials for a service."""
    manager = get_credentials_manager()

    # Save all credentials for the service
    manager.save_credentials(service, request.credentials)

    return {"status": "success", "message": f"Credentials saved for {service}"}


@router.post("/{service}/api-key")
async def set_api_key(service: str, request: SetApiKeyRequest) -> dict[str, str]:
    """Set an API key for a service."""
    if request.service != service:
        raise HTTPException(status_code=400, detail="Service mismatch in request")

    manager = get_credentials_manager()
    manager.set_api_key(service, request.api_key, request.key_name)

    return {"status": "success", "message": f"API key set for {service}"}


@router.get("/{service}/api-key")
async def get_api_key(service: str, key_name: str = "api_key") -> dict[str, Any]:
    """Get the API key for a service (returns only existence status for security)."""
    manager = get_credentials_manager()
    api_key = manager.get_api_key(service, key_name)

    if api_key:
        # Don't return the actual key for security
        return {
            "service": service,
            "has_key": True,
            "key_name": key_name,
            # Return masked version
            "masked_key": f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****",
        }

    return {"service": service, "has_key": False, "key_name": key_name}


@router.get("/{service}")
async def get_credentials(service: str) -> dict[str, Any]:
    """Get credentials for a service (for editing)."""
    manager = get_credentials_manager()
    credentials = manager.load_credentials(service)

    if not credentials:
        return {"service": service, "credentials": {}}

    return {"service": service, "credentials": credentials}


@router.delete("/{service}")
async def delete_credentials(service: str) -> dict[str, str]:
    """Delete all credentials for a service."""
    manager = get_credentials_manager()
    manager.delete_credentials(service)

    return {"status": "success", "message": f"Credentials deleted for {service}"}


@router.post("/{service}/test")
async def test_credentials(service: str) -> dict[str, Any]:
    """Test if credentials are valid for a service."""
    # This is a placeholder - actual testing would depend on the service
    manager = get_credentials_manager()
    credentials = manager.load_credentials(service)

    if not credentials:
        raise HTTPException(status_code=404, detail=f"No credentials found for {service}")

    # For now, just check if credentials exist
    # In the future, we could implement actual validation per service
    return {
        "service": service,
        "status": "success",
        "message": "Credentials exist (validation not implemented)",
    }

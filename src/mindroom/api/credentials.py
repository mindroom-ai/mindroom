"""Unified credentials management API."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mindroom.credentials import CredentialsManager, get_credentials_manager, validate_service_name

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


def _filter_internal_keys(credentials: dict[str, Any]) -> dict[str, Any]:
    """Remove internal metadata keys (prefixed with _) from credentials."""
    return {k: v for k, v in credentials.items() if not k.startswith("_")}


def _validated_service(service: str) -> str:
    try:
        return validate_service_name(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _manager_for_worker(worker_key: str | None) -> CredentialsManager:
    normalized_worker_key = worker_key.strip() if worker_key is not None else ""
    manager = get_credentials_manager()
    if not normalized_worker_key:
        return manager
    return manager.for_worker(normalized_worker_key)


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
async def list_services(worker_key: str | None = None) -> list[str]:
    """List all services with stored credentials."""
    manager = _manager_for_worker(worker_key)
    return manager.list_services()


@router.get("/{service}/status")
async def get_credential_status(service: str, worker_key: str | None = None) -> CredentialStatus:
    """Get the status of credentials for a service."""
    service = _validated_service(service)
    manager = _manager_for_worker(worker_key)
    credentials = manager.load_credentials(service)

    if credentials:
        filtered = _filter_internal_keys(credentials) if isinstance(credentials, dict) else {}
        return CredentialStatus(
            service=service,
            has_credentials=True,
            key_names=list(filtered.keys()) if filtered else None,
        )

    return CredentialStatus(service=service, has_credentials=False)


@router.post("/{service}")
async def set_credentials(
    service: str,
    request: SetCredentialsRequest,
    worker_key: str | None = None,
) -> dict[str, str]:
    """Set multiple credentials for a service."""
    service = _validated_service(service)
    manager = _manager_for_worker(worker_key)

    # Mark as UI-sourced and save
    creds = {**request.credentials, "_source": "ui"}
    manager.save_credentials(service, creds)

    return {"status": "success", "message": f"Credentials saved for {service}"}


@router.post("/{service}/api-key")
async def set_api_key(
    service: str,
    request: SetApiKeyRequest,
    worker_key: str | None = None,
) -> dict[str, str]:
    """Set an API key for a service."""
    service = _validated_service(service)
    request_service = _validated_service(request.service)
    if request_service != service:
        raise HTTPException(status_code=400, detail="Service mismatch in request")

    manager = _manager_for_worker(worker_key)
    credentials = manager.load_credentials(service) or {}
    credentials[request.key_name] = request.api_key
    credentials["_source"] = "ui"
    manager.save_credentials(service, credentials)

    return {"status": "success", "message": f"API key set for {service}"}


@router.get("/{service}/api-key")
async def get_api_key(
    service: str,
    key_name: str = "api_key",
    include_value: bool = False,
    worker_key: str | None = None,
) -> dict[str, Any]:
    """Get API key metadata for a service, and optionally the full key value."""
    service = _validated_service(service)
    manager = _manager_for_worker(worker_key)
    credentials = manager.load_credentials(service) or {}
    api_key = credentials.get(key_name)

    if api_key:
        source = credentials.get("_source")
        response = {
            "service": service,
            "has_key": True,
            "key_name": key_name,
            # Return masked version
            "masked_key": f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****",
            "source": source,
        }
        if include_value:
            response["api_key"] = api_key
        return response

    return {"service": service, "has_key": False, "key_name": key_name}


@router.get("/{service}")
async def get_credentials(service: str, worker_key: str | None = None) -> dict[str, Any]:
    """Get credentials for a service (for editing)."""
    service = _validated_service(service)
    manager = _manager_for_worker(worker_key)
    credentials = manager.load_credentials(service)

    if not credentials:
        return {"service": service, "credentials": {}}

    return {"service": service, "credentials": _filter_internal_keys(credentials)}


@router.delete("/{service}")
async def delete_credentials(service: str, worker_key: str | None = None) -> dict[str, str]:
    """Delete all credentials for a service."""
    service = _validated_service(service)
    manager = _manager_for_worker(worker_key)
    manager.delete_credentials(service)

    return {"status": "success", "message": f"Credentials deleted for {service}"}


@router.post("/{service}/copy-from/{source_service}")
async def copy_credentials(
    service: str,
    source_service: str,
    worker_key: str | None = None,
    source_worker_key: str | None = None,
) -> dict[str, str]:
    """Copy credentials from one service to another."""
    service = _validated_service(service)
    source_service = _validated_service(source_service)
    source_manager = _manager_for_worker(source_worker_key)
    source_creds = source_manager.load_credentials(source_service)

    if not source_creds:
        raise HTTPException(status_code=404, detail=f"No credentials found for {source_service}")

    # Copy credentials, marking as UI-sourced
    target_creds = {k: v for k, v in source_creds.items() if not k.startswith("_")}
    target_creds["_source"] = "ui"
    manager = _manager_for_worker(worker_key)
    manager.save_credentials(service, target_creds)

    return {"status": "success", "message": f"Credentials copied from {source_service} to {service}"}


@router.post("/{service}/test")
async def validate_credentials(service: str, worker_key: str | None = None) -> dict[str, Any]:
    """Test if credentials are valid for a service."""
    service = _validated_service(service)
    # This is a placeholder - actual testing would depend on the service
    manager = _manager_for_worker(worker_key)
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

"""Unified credentials management API routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mindroom.api.credential_responses import filter_credentials_for_response, filter_oauth_client_config_for_response
from mindroom.api.credentials_oauth_policy import (
    OAuthCredentialServices,
    dashboard_credentials_for_save,
    dashboard_may_edit_oauth_match,
    is_client_config_service,
    oauth_service_match,
    oauth_services_for_request,
    preserve_oauth_client_config_from_existing,
    reject_oauth_api_key_read_field,
    reject_oauth_api_key_write_field,
    reject_oauth_client_config_copy,
    reject_oauth_credentials_document,
    reject_oauth_token_service,
    validate_oauth_client_config_fields,
)
from mindroom.api.credentials_target import (
    RequestCredentialsTarget,
    delete_credentials_for_target,
    load_credentials_for_target,
    primary_runtime_scoped_services_for_target,
    request_may_target_scoped_credentials,
    resolve_request_credentials_target,
    save_credentials_for_target,
)
from mindroom.credential_policy import credential_service_policy
from mindroom.credentials import list_worker_grantable_shared_services, validate_service_name
from mindroom.tool_system.worker_routing import unsupported_shared_only_integration_names

if TYPE_CHECKING:
    from mindroom.api.credentials_oauth_policy import OAuthCredentialServiceMatch

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


def _validated_service(service: str) -> str:
    try:
        return validate_service_name(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@dataclass(frozen=True)
class _DashboardCredentialAccess:
    """Credential storage access for one dashboard request target."""

    target: RequestCredentialsTarget
    oauth_services: OAuthCredentialServices

    @classmethod
    def resolve(
        cls,
        request: Request,
        *,
        agent_name: str | None,
        service_names: tuple[str, ...] = (),
        allow_private_scopes: bool = False,
    ) -> _DashboardCredentialAccess:
        """Resolve dashboard credential access for one request."""
        oauth_services = oauth_services_for_request(request)
        # Token services are rejected below, but they still need target resolution
        # first so agent-scoped requests run authorization before route-specific 400s.
        oauth_service_requires_target_resolution = any(
            oauth_services.match(service) is not None for service in service_names
        ) and request_may_target_scoped_credentials(request, agent_name)
        target = resolve_request_credentials_target(
            request,
            agent_name=agent_name,
            service_names=service_names,
            allow_private_scopes=allow_private_scopes or oauth_service_requires_target_resolution,
        )
        oauth_services.reject_non_editable_services(service_names)
        return cls(target=target, oauth_services=oauth_services)

    def match(self, service: str) -> OAuthCredentialServiceMatch | None:
        """Return the OAuth role for one credential service, if registered."""
        return self.oauth_services.match(service)

    def reject_token_service(self, service: str) -> None:
        """Reject direct dashboard access to OAuth token credentials."""
        reject_oauth_token_service(self.match(service))

    def reject_stored_oauth_credentials(self, credentials: dict[str, Any]) -> None:
        """Reject stored OAuth token documents returned through generic routes."""
        reject_oauth_credentials_document(credentials)

    def load(self, service: str) -> dict[str, Any] | None:
        """Load dashboard-visible credentials for one service."""
        self.reject_token_service(service)
        return load_credentials_for_target(service, self.target)

    def save(self, service: str, credentials: dict[str, Any]) -> None:
        """Save dashboard-visible credentials for one service."""
        self.reject_token_service(service)
        try:
            save_credentials_for_target(service, credentials, self.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def delete(self, service: str) -> None:
        """Delete dashboard-visible credentials for one service."""
        self.reject_token_service(service)
        delete_credentials_for_target(service, self.target)

    def response_credentials(self, service: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """Return credentials filtered for dashboard responses."""
        if is_client_config_service(service, self.match(service)):
            return filter_oauth_client_config_for_response(credentials)
        return filter_credentials_for_response(
            credentials,
            is_oauth_service=dashboard_may_edit_oauth_match(self.match(service)),
        )

    def credentials_for_save(self, service: str, config_values: dict[str, Any]) -> dict[str, Any]:
        """Return user-submitted credentials normalized for storage."""
        match = self.match(service)
        credentials = dashboard_credentials_for_save(
            config_values,
            strip_oauth_fields=dashboard_may_edit_oauth_match(match) and not is_client_config_service(service, match),
        )
        if is_client_config_service(service, match):
            validate_oauth_client_config_fields(credentials)
            existing_credentials = load_credentials_for_target(service, self.target) or {}
            preserve_oauth_client_config_from_existing(credentials, existing_credentials, match)
            return credentials
        return credentials

    def list_services(self) -> list[str]:
        """List dashboard-visible services for the resolved target."""
        if self.target.worker_scope is None:
            return [
                service
                for service in self.target.target_manager.list_services()
                if self.oauth_services.dashboard_may_show_service(service)
            ]
        worker_services = set(self.target.target_manager.list_services())
        primary_runtime_global_services = {
            service
            for service in self.target.base_manager.list_services()
            if credential_service_policy(service, self.target.worker_scope).uses_primary_runtime_global_credentials
        }
        primary_runtime_services = primary_runtime_scoped_services_for_target(self.target)
        shared_manager = self.target.base_manager.shared_manager()
        shared_services = set(
            list_worker_grantable_shared_services(
                shared_manager=shared_manager,
                allowed_services=self.target.allowed_shared_services or frozenset(),
            ),
        )
        if self.target.worker_scope == "shared":
            shared_services |= {
                service
                for service in shared_manager.list_services()
                if credential_service_policy(service, self.target.worker_scope).uses_local_shared_credentials
            }
        services = worker_services | primary_runtime_global_services | primary_runtime_services | shared_services
        services -= set(unsupported_shared_only_integration_names(sorted(services), self.target.worker_scope))
        return sorted(service for service in services if self.oauth_services.dashboard_may_show_service(service))


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
async def list_services(
    request: Request,
    agent_name: str | None = None,
) -> list[str]:
    """List all services with stored credentials."""
    access = _DashboardCredentialAccess.resolve(request, agent_name=agent_name, allow_private_scopes=True)
    return access.list_services()


@router.get("/{service}/status")
async def get_credential_status(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> CredentialStatus:
    """Get the status of credentials for a service."""
    service = _validated_service(service)
    access = _DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    credentials = access.load(service)

    if credentials:
        access.reject_stored_oauth_credentials(credentials)
        filtered = access.response_credentials(service, credentials)
        return CredentialStatus(
            service=service,
            has_credentials=True,
            key_names=list(filtered.keys()) if filtered else None,
        )

    return CredentialStatus(service=service, has_credentials=False)


@router.post("/{service}")
async def set_credentials(
    service: str,
    http_request: Request,
    payload: SetCredentialsRequest,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Set multiple credentials for a service."""
    service = _validated_service(service)
    reject_oauth_credentials_document(payload.credentials)
    access = _DashboardCredentialAccess.resolve(
        http_request,
        agent_name=agent_name,
        service_names=(service,),
    )
    existing_credentials = access.load(service)
    if existing_credentials:
        access.reject_stored_oauth_credentials(existing_credentials)

    creds = access.credentials_for_save(service, payload.credentials)
    access.save(service, creds)

    return {"status": "success", "message": f"Credentials saved for {service}"}


@router.post("/{service}/api-key")
async def set_api_key(
    service: str,
    http_request: Request,
    payload: SetApiKeyRequest,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Set an API key for a service."""
    service = _validated_service(service)
    request_service = _validated_service(payload.service)
    if request_service != service:
        raise HTTPException(status_code=400, detail="Service mismatch in request")
    access = _DashboardCredentialAccess.resolve(
        http_request,
        agent_name=agent_name,
        service_names=(service,),
    )
    reject_oauth_api_key_write_field(service, access.match(service), key_name=payload.key_name)

    credentials = access.load(service) or {}
    access.reject_stored_oauth_credentials(credentials)
    credentials[payload.key_name] = payload.api_key
    credentials["_source"] = "ui"
    access.save(service, credentials)

    return {"status": "success", "message": f"API key set for {service}"}


@router.get("/{service}/api-key")
async def get_api_key(
    service: str,
    request: Request,
    key_name: str = "api_key",
    include_value: bool = False,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Get API key metadata for a service, and optionally the full key value."""
    service = _validated_service(service)
    access = _DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    oauth_match = access.match(service)
    reject_oauth_api_key_read_field(service, oauth_match, key_name=key_name)
    credentials = access.load(service) or {}
    access.reject_stored_oauth_credentials(credentials)
    api_key = credentials.get(key_name)
    if is_client_config_service(service, oauth_match) and api_key is not None and not isinstance(api_key, str):
        raise HTTPException(
            status_code=400,
            detail=f"OAuth client config field '{key_name}' is not a readable string.",
        )

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
async def get_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Get credentials for a service (for editing)."""
    service = _validated_service(service)
    access = _DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    credentials = access.load(service)

    if not credentials:
        return {"service": service, "credentials": {}}
    access.reject_stored_oauth_credentials(credentials)

    return {
        "service": service,
        "credentials": access.response_credentials(service, credentials),
    }


@router.delete("/{service}")
async def delete_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Delete all credentials for a service."""
    service = _validated_service(service)
    access = _DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    existing_credentials = access.load(service)
    if existing_credentials:
        access.reject_stored_oauth_credentials(existing_credentials)
    access.delete(service)

    return {"status": "success", "message": f"Credentials deleted for {service}"}


@router.post("/{service}/copy-from/{source_service}")
async def copy_credentials(
    service: str,
    source_service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Copy credentials from one service to another."""
    service = _validated_service(service)
    source_service = _validated_service(source_service)
    access = _DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service, source_service),
    )
    destination_match = oauth_service_match(request, service)
    source_match = oauth_service_match(request, source_service)
    reject_oauth_client_config_copy(source_service, source_match, service, destination_match)
    source_creds = access.load(source_service)
    destination_creds = access.load(service)

    if not source_creds:
        raise HTTPException(status_code=404, detail=f"No credentials found for {source_service}")
    reject_oauth_credentials_document(source_creds)
    if destination_creds:
        reject_oauth_credentials_document(destination_creds)

    # Copy credentials, marking as UI-sourced
    target_creds = {k: v for k, v in source_creds.items() if not k.startswith("_")}
    target_creds["_source"] = "ui"
    access.save(service, target_creds)

    return {"status": "success", "message": f"Credentials copied from {source_service} to {service}"}


@router.post("/{service}/test")
async def validate_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Test if credentials are valid for a service."""
    service = _validated_service(service)
    # This is a placeholder - actual testing would depend on the service
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    reject_oauth_token_service(oauth_service_match(request, service))
    credentials = load_credentials_for_target(service, target)

    if not credentials:
        raise HTTPException(status_code=404, detail=f"No credentials found for {service}")
    reject_oauth_credentials_document(credentials)

    # For now, just check if credentials exist
    # In the future, we could implement actual validation per service
    return {
        "service": service,
        "status": "success",
        "message": "Credentials exist (validation not implemented)",
    }

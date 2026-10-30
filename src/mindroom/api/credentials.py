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
    dashboard_candidate_stores,
    delete_credentials_for_target,
    load_credentials_for_target,
    loaded_runtime_config_for_credentials_request,
    request_may_target_scoped_credentials,
    resolve_request_credentials_target,
    save_credentials_for_target,
    target_reads_service_from,
)
from mindroom.credentials.manager import validate_service_name
from mindroom.credentials.policy import is_egress_broker_service, is_oauth_token_service
from mindroom.credentials.sync import canonical_provider_service, resolve_provider_service_api_key
from mindroom.embeddings.embedder_health import handle_embedder_credential_change
from mindroom.embeddings.factory import embedder_client_signature
from mindroom.tool_system.worker_routing import unsupported_shared_only_integration_names

if TYPE_CHECKING:
    from mindroom.api.credentials_oauth_policy import OAuthCredentialServiceMatch
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


@dataclass(frozen=True)
class _ActiveEmbedderRuntime:
    """Committed embedder client identity and the config needed to re-probe it."""

    config: Config
    runtime_paths: RuntimePaths
    client_signature: str


def _validated_service(service: str) -> str:
    try:
        validated = validate_service_name(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_egress_broker_service(validated):
        msg = "Egress broker secrets are managed through /api/egress-broker."
        raise HTTPException(status_code=400, detail=msg)

    return validated


def _save_service(access: _DashboardCredentialAccess, service: str) -> str:
    """Return the service to write, storing new env-var-named provider keys under the canonical provider.

    An env-var-named service that already exists keeps receiving writes, because
    config may reference it by exact name (for example an embedder
    ``credentials_service``), so rotating it must update that service.
    """
    canonical_service = canonical_provider_service(service)
    if canonical_service == service or access.load(service) is not None:
        return service
    return canonical_service


def _active_embedder_runtime(request: Request, access: _DashboardCredentialAccess) -> _ActiveEmbedderRuntime | None:
    """Resolve the concrete embedder client identity for the primary runtime."""
    if access.target.worker_scope is not None:
        return None
    runtime_config = loaded_runtime_config_for_credentials_request(request)
    if runtime_config is None:
        return None
    config, runtime_paths = runtime_config
    return _ActiveEmbedderRuntime(
        config=config,
        runtime_paths=runtime_paths,
        client_signature=embedder_client_signature(config, runtime_paths),
    )


def _handle_runtime_credential_change(
    request: Request,
    access: _DashboardCredentialAccess,
    previous_runtime: _ActiveEmbedderRuntime | None,
) -> None:
    """Invalidate and re-probe only when the resolved embedder client changed."""
    if previous_runtime is None:
        return
    current_runtime = _active_embedder_runtime(request, access)
    if current_runtime is None:
        handle_embedder_credential_change()
    elif current_runtime.client_signature != previous_runtime.client_signature:
        handle_embedder_credential_change(current_runtime.config, current_runtime.runtime_paths)


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
            is_oauth_token_service(service) or oauth_services.match(service) is not None for service in service_names
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

    def load(self, service: str) -> dict[str, Any] | None:
        """Load dashboard-visible credentials for one service."""
        reject_oauth_token_service(service)
        try:
            return load_credentials_for_target(service, self.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def save(self, service: str, credentials: dict[str, Any]) -> None:
        """Save dashboard-visible credentials for one service."""
        reject_oauth_token_service(service)
        try:
            save_credentials_for_target(service, credentials, self.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def delete(self, service: str) -> None:
        """Delete dashboard-visible credentials for one service."""
        reject_oauth_token_service(service)
        try:
            delete_credentials_for_target(service, self.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def response_credentials(self, service: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """Return credentials filtered for dashboard responses."""
        match = self.match(service)
        if is_client_config_service(service, match):
            return filter_oauth_client_config_for_response(credentials)
        return filter_credentials_for_response(
            credentials,
            is_oauth_service=dashboard_may_edit_oauth_match(match),
            oauth_fallback_fields=match.oauth_fallback_fields if match is not None else frozenset(),
        )

    def credentials_for_save(
        self,
        service: str,
        config_values: dict[str, Any],
        existing_credentials: dict[str, Any],
    ) -> dict[str, Any]:
        """Return user-submitted credentials normalized for storage over the service's ``existing_credentials``."""
        match = self.match(service)
        credentials = dashboard_credentials_for_save(
            config_values,
            strip_oauth_fields=dashboard_may_edit_oauth_match(match) and not is_client_config_service(service, match),
            oauth_fallback_fields=match.oauth_fallback_fields if match is not None else frozenset(),
        )
        if is_client_config_service(service, match):
            validate_oauth_client_config_fields(credentials)
            preserve_oauth_client_config_from_existing(credentials, existing_credentials, match)
        return credentials

    def _dashboard_may_list(self, service: str) -> bool:
        # Egress secrets are managed through /api/egress-broker, so the generic routes reject them.
        return not is_egress_broker_service(service) and self.oauth_services.dashboard_may_show_service(service)

    def list_services(self) -> list[str]:
        """List dashboard-visible services stored where the resolved target reads them."""
        services = {
            service
            for store in dashboard_candidate_stores(self.target)
            for service in store.list_services()
            if self._dashboard_may_list(service) and target_reads_service_from(service, store, self.target)
        }
        services -= set(unsupported_shared_only_integration_names(sorted(services), self.target.worker_scope))
        return sorted(services)


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
        reject_oauth_credentials_document(credentials)
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
    service = _save_service(access, service)
    existing_credentials = access.load(service)
    if existing_credentials:
        reject_oauth_credentials_document(existing_credentials)
    previous_embedder_runtime = _active_embedder_runtime(http_request, access)

    creds = access.credentials_for_save(service, payload.credentials, existing_credentials or {})
    access.save(service, creds)
    _handle_runtime_credential_change(http_request, access, previous_embedder_runtime)

    return {"status": "success", "service": service, "message": f"Credentials saved for {service}"}


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
    service = save_dashboard_api_key(
        http_request,
        service,
        payload.api_key,
        key_name=payload.key_name,
        agent_name=agent_name,
    )
    return {"status": "success", "service": service, "message": f"API key set for {service}"}


def save_dashboard_api_key(
    http_request: Request,
    service: str,
    api_key: str,
    *,
    key_name: str = "api_key",
    agent_name: str | None = None,
) -> str:
    """Save one UI-sourced API key through the dashboard credential access rules and return the stored service."""
    access = _DashboardCredentialAccess.resolve(
        http_request,
        agent_name=agent_name,
        service_names=(service,),
    )
    service = _save_service(access, service)
    reject_oauth_api_key_write_field(service, access.match(service), key_name=key_name)

    credentials = access.load(service) or {}
    reject_oauth_credentials_document(credentials)
    previous_embedder_runtime = _active_embedder_runtime(http_request, access)
    credentials[key_name] = api_key
    credentials["_source"] = "ui"
    access.save(service, credentials)
    _handle_runtime_credential_change(http_request, access, previous_embedder_runtime)
    return service


def _load_api_key_credentials(
    access: _DashboardCredentialAccess,
    service: str,
    key_name: str,
) -> tuple[str, dict[str, Any]]:
    """Load the credentials the runtime reads this service's key from, and the service that holds them.

    Provider API keys resolve like the runtime does: the canonical service first,
    then its env-var-named twin (for example ``ANTHROPIC_API_KEY``).
    """
    loaded: dict[str, dict[str, Any]] = {}

    def load(candidate: str) -> dict[str, Any]:
        if candidate not in loaded:
            credentials = access.load(candidate) or {}
            reject_oauth_credentials_document(credentials)
            loaded[candidate] = credentials
        return loaded[candidate]

    if key_name != "api_key":
        return service, load(service)

    def load_api_key(candidate: str) -> str | None:
        api_key = load(candidate).get("api_key")
        return api_key if isinstance(api_key, str) else None

    credential_service, _ = resolve_provider_service_api_key(service, load_api_key)
    return credential_service, load(credential_service)


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
    credential_service, credentials = _load_api_key_credentials(access, service, key_name)
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
            "credential_service": credential_service,
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
    reject_oauth_credentials_document(credentials)

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
        reject_oauth_credentials_document(existing_credentials)
    previous_embedder_runtime = _active_embedder_runtime(request, access)
    access.delete(service)
    _handle_runtime_credential_change(request, access, previous_embedder_runtime)

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
    service = _save_service(access, service)
    destination_match = access.match(service)
    source_match = access.match(source_service)
    reject_oauth_client_config_copy(source_service, source_match, service, destination_match)
    source_creds = access.load(source_service)
    destination_creds = access.load(service)

    if not source_creds:
        raise HTTPException(status_code=404, detail=f"No credentials found for {source_service}")
    reject_oauth_credentials_document(source_creds)
    if destination_creds:
        reject_oauth_credentials_document(destination_creds)

    previous_embedder_runtime = _active_embedder_runtime(request, access)
    # Copy credentials, marking as UI-sourced
    target_creds = {k: v for k, v in source_creds.items() if not k.startswith("_")}
    target_creds["_source"] = "ui"
    access.save(service, target_creds)
    _handle_runtime_credential_change(request, access, previous_embedder_runtime)

    return {
        "status": "success",
        "service": service,
        "message": f"Credentials copied from {source_service} to {service}",
    }


@router.post("/{service}/test")
async def validate_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Check stored credentials exist without validating them with the provider."""
    service = _validated_service(service)
    access = _DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    credentials = access.load(service)

    if not credentials:
        raise HTTPException(status_code=404, detail=f"No credentials found for {service}")
    reject_oauth_credentials_document(credentials)

    return {
        "service": service,
        "status": "success",
        "message": "Credentials exist (validation not implemented)",
    }

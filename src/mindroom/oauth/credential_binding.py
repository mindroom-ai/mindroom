"""Canonical credential target binding for OAuth connect and reset workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerKeyScope, ResolvedWorkerTarget


@dataclass(frozen=True, slots=True)
class OAuthCredentialBinding:
    """Provider and resolved worker target carried by an OAuth workflow."""

    provider_id: str
    credential_service: str
    requested_agent_name: str | None
    worker_scope: ResolvedWorkerKeyScope
    worker_key: str


class OAuthCredentialBindingParseError(ValueError):
    """A serialized OAuth workflow binding does not match its expected target."""

    reason: Literal["provider_mismatch", "invalid_target"]

    def __init__(self, reason: Literal["provider_mismatch", "invalid_target"]) -> None:
        self.reason = reason
        super().__init__("Invalid OAuth credential binding")


def oauth_credential_binding(
    provider: OAuthProvider,
    worker_target: ResolvedWorkerTarget | None,
) -> OAuthCredentialBinding:
    """Derive one workflow binding from a provider and resolved worker target."""
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    worker_key = worker_target.worker_key if worker_target is not None and worker_target.worker_key else ""
    resolved_scope = worker_scope or "unscoped"
    return OAuthCredentialBinding(provider.id, provider.credential_service, agent_name, resolved_scope, worker_key)


def oauth_credential_binding_payload(binding: OAuthCredentialBinding) -> dict[str, str]:
    """Serialize one OAuth workflow binding without changing its public payload shape."""
    return {
        "provider": binding.provider_id,
        "credential_service": binding.credential_service,
        "agent_name": binding.requested_agent_name or "",
        "worker_scope": binding.worker_scope,
        "worker_key": binding.worker_key,
    }


def parse_oauth_credential_binding_payload(
    provider: OAuthProvider,
    payload: Mapping[str, object],
    *,
    allowed_worker_scopes: frozenset[ResolvedWorkerKeyScope],
    require_agent_name: bool,
    require_worker_key: bool,
) -> OAuthCredentialBinding:
    """Parse and validate one untrusted OAuth workflow binding payload."""
    if payload.get("provider") != provider.id or payload.get("credential_service") != provider.credential_service:
        reason = "provider_mismatch"
        raise OAuthCredentialBindingParseError(reason)

    agent_name = payload.get("agent_name")
    worker_scope = payload.get("worker_scope")
    worker_key = payload.get("worker_key")
    if (
        not isinstance(agent_name, str)
        or (require_agent_name and not agent_name)
        or not isinstance(worker_scope, str)
        or worker_scope not in allowed_worker_scopes
        or not isinstance(worker_key, str)
        or (require_worker_key and not worker_key)
    ):
        reason = "invalid_target"
        raise OAuthCredentialBindingParseError(reason)

    resolved_scope = cast("ResolvedWorkerKeyScope", worker_scope)
    return OAuthCredentialBinding(
        provider.id,
        provider.credential_service,
        agent_name or None,
        resolved_scope,
        worker_key,
    )

"""Resolve and freeze agent-initiated OAuth credential reset targets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot,
    oauth_reset_operation_result_for_target,
    resolve_oauth_credential_context,
)
from mindroom.oauth.registry import load_oauth_providers
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, WorkerScope

_OAUTH_RESET_TOOL_NAME = "reset_oauth_connection"


class OAuthResetTargetError(ValueError):
    """One requested provider cannot resolve to a safe agent credential target."""


class OAuthResetApprovalBindingError(RuntimeError):
    """One approved OAuth reset no longer resolves to its original credential target."""


@dataclass(frozen=True, slots=True)
class _ResolvedOAuthResetTarget:
    """Exact provider and requester-isolated credential target for one reset."""

    agent_name: str
    credential_context: OAuthCredentialContext

    @property
    def provider(self) -> OAuthProvider:
        """Return the provider bound to this reset."""
        return self.credential_context.provider

    @property
    def worker_target(self) -> ResolvedWorkerTarget:
        """Return the requester-isolated target bound to this reset."""
        worker_target = self.credential_context.worker_target
        assert worker_target is not None
        return worker_target


def resolve_oauth_reset_target(
    provider_id: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    worker_target: ResolvedWorkerTarget | None = None,
) -> _ResolvedOAuthResetTarget:
    """Resolve one configured provider to the exact credential target it may reset."""
    if agent_name not in config.agents:
        msg = "OAuth reset is available only during an agent request."
        raise OAuthResetTargetError(msg)

    tool_metadata = resolved_tool_metadata_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=True,
    )
    allowed_provider_ids = {
        metadata.auth_provider
        for tool_name in config.resolve_entity(agent_name).available_tools
        if (metadata := tool_metadata.get(tool_name)) is not None and metadata.auth_provider is not None
    }
    if provider_id not in allowed_provider_ids:
        available = ", ".join(sorted(allowed_provider_ids)) or "none"
        msg = f"Provider {provider_id!r} is not available to this agent. Available providers: {available}."
        raise OAuthResetTargetError(msg)

    provider = load_oauth_providers(config, runtime_paths).get(provider_id)
    if provider is None:
        msg = f"OAuth provider {provider_id!r} is not configured."
        raise OAuthResetTargetError(msg)

    resolved_worker_target = worker_target
    if resolved_worker_target is None:
        resolved_worker_target = build_agent_toolkit_worker_target(
            config.resolve_entity(agent_name).execution_scope,
            agent_name,
            is_private=config.get_agent(agent_name).private is not None,
            execution_identity=execution_identity,
            runtime_paths=runtime_paths,
        )
    if resolved_worker_target.routing_agent_name != agent_name:
        msg = "OAuth reset target does not belong to the invoking agent."
        raise OAuthResetTargetError(msg)

    credential_context = resolve_oauth_credential_context(
        provider,
        runtime_paths,
        get_runtime_credentials_manager(runtime_paths),
        resolved_worker_target,
        execution_identity=execution_identity,
        authorization=config.authorization,
    )
    credential_target = credential_context.worker_target
    if (
        credential_target is None
        or credential_target.worker_scope not in {"user", "user_agent"}
        or credential_target.worker_key is None
    ):
        msg = "Agent-initiated OAuth reset requires a requester-isolated user or user_agent scope."
        raise OAuthResetTargetError(msg)
    return _ResolvedOAuthResetTarget(
        agent_name=agent_name,
        credential_context=credential_context,
    )


async def build_oauth_reset_approval_bindings(
    identified_tools: Sequence[tuple[ToolExecution, str, str, str]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> dict[str, dict[str, object]]:
    """Freeze exact credential targets for every OAuth reset in one approval pause."""
    bindings: dict[str, dict[str, object]] = {}
    for tool, tool_call_id, tool_name, invoking_agent in identified_tools:
        if tool_name != _OAUTH_RESET_TOOL_NAME:
            continue
        if execution_identity is None:
            msg = "OAuth reset approval requires an execution identity."
            raise OAuthResetApprovalBindingError(msg)
        tool_args = tool.tool_args
        provider_id = tool_args.get("provider_id") if isinstance(tool_args, dict) else None
        if not isinstance(provider_id, str) or not provider_id:
            msg = "OAuth reset approval requires a provider_id."
            raise OAuthResetApprovalBindingError(msg)
        try:
            target = resolve_oauth_reset_target(
                provider_id,
                agent_name=invoking_agent,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
        except OAuthResetTargetError as exc:
            raise OAuthResetApprovalBindingError(str(exc)) from exc
        bindings[tool_call_id] = await _oauth_reset_target_binding(target)
    return bindings


async def validate_oauth_reset_approval_bindings(
    *,
    calls: Sequence[tuple[str, str, str, bool]],
    bindings: Mapping[str, Mapping[str, object]],
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    allow_connection_generation_drift: bool = False,
) -> None:
    """Fail closed when an approved reset resolves differently after configuration drift."""
    for tool_call_id, tool_name, invoking_agent, approved in calls:
        if tool_name != _OAUTH_RESET_TOOL_NAME or not approved:
            continue
        stored_binding = bindings.get(tool_call_id)
        nested_binding = stored_binding.get("oauth_reset") if stored_binding is not None else None
        binding = cast("Mapping[str, object]", nested_binding) if isinstance(nested_binding, dict) else stored_binding
        provider_id = binding.get("provider_id") if binding is not None else None
        if not isinstance(provider_id, str) or not provider_id:
            msg = "Approved OAuth credential target is missing; run the reset again."
            raise OAuthResetApprovalBindingError(msg)
        try:
            target = resolve_oauth_reset_target(
                provider_id,
                agent_name=invoking_agent,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
        except OAuthResetTargetError as exc:
            msg = "Approved OAuth credential target changed or is unavailable; run the reset again."
            raise OAuthResetApprovalBindingError(msg) from exc
        assert binding is not None
        if allow_connection_generation_drift:
            ignored_keys = {"credential_generation", "connection_generation"}
            current_binding = _oauth_reset_target_identity_binding(target)
        else:
            ignored_keys = {"credential_generation"}
            current_binding = await _oauth_reset_target_binding(target)
        binding = {key: value for key, value in binding.items() if key not in ignored_keys}
        current_binding = {key: value for key, value in current_binding.items() if key not in ignored_keys}
        if binding != current_binding:
            msg = "Approved OAuth credential target changed; run the reset again."
            raise OAuthResetApprovalBindingError(msg)


async def _oauth_reset_target_binding(target: _ResolvedOAuthResetTarget) -> dict[str, object]:
    snapshot = await load_oauth_credentials_snapshot(target.credential_context)
    return {
        **_oauth_reset_target_identity_binding(target),
        "credential_generation": snapshot.generation,
        "connection_generation": snapshot.connection_generation,
    }


def _oauth_reset_target_identity_binding(target: _ResolvedOAuthResetTarget) -> dict[str, object]:
    """Return the canonical reset target without reading mutable credential state."""
    worker_target = target.worker_target
    execution_identity = worker_target.execution_identity
    credential_requester_id = execution_identity.requester_id if execution_identity is not None else None
    if not credential_requester_id:
        msg = "Agent-initiated OAuth reset requires a canonical requester identity."
        raise OAuthResetApprovalBindingError(msg)
    return {
        "provider_id": target.provider.id,
        "credential_service": target.provider.credential_service,
        "agent_name": target.agent_name,
        "worker_scope": cast("str", worker_target.worker_scope),
        "worker_key": cast("str", worker_target.worker_key),
        "routing_agent_name": cast("str", worker_target.routing_agent_name),
        "credential_requester_id": credential_requester_id,
    }


def completed_oauth_reset_result_from_binding(
    binding: Mapping[str, object],
    *,
    execution_identity: ToolExecutionIdentity,
    runtime_paths: RuntimePaths,
    operation_id: str,
) -> bool | None:
    """Read completed reset debt from one frozen scope without live provider resolution."""
    credential_service = binding.get("credential_service")
    worker_scope = binding.get("worker_scope")
    worker_key = binding.get("worker_key")
    routing_agent_name = binding.get("routing_agent_name")
    credential_requester_id = binding.get("credential_requester_id")
    if (
        not isinstance(credential_service, str)
        or not credential_service.endswith("_oauth")
        or worker_scope not in {"user", "user_agent"}
        or not isinstance(worker_key, str)
        or not worker_key
        or not isinstance(routing_agent_name, str)
        or not routing_agent_name
        or not isinstance(credential_requester_id, str)
        or not credential_requester_id
    ):
        return None
    frozen_identity = replace(
        execution_identity,
        agent_name=routing_agent_name,
        requester_id=credential_requester_id,
    )
    frozen_target = resolve_worker_target(
        cast("WorkerScope", worker_scope),
        routing_agent_name,
        frozen_identity,
    )
    if frozen_target.worker_key != worker_key:
        return None
    return oauth_reset_operation_result_for_target(
        credential_service,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=frozen_target,
        operation_id=operation_id,
    )

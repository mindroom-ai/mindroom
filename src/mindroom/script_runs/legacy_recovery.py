"""Compatibility for historical durable script authority digests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from mindroom.script_runs.compatibility import SCRIPT_PROTOCOL_VERSION

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.backends.kubernetes_config import KubernetesWorkerBackendConfig


# LEGACY_COMPAT: Unversioned recovery digests covering the complete Kubernetes config.
# Legacy format: unversioned recovery digests included the complete Kubernetes config snapshot.
# Last legacy release: v2026.9.142; v2 binds only the owning script worker's process authority.
# Handling: migrate only an exact digest for the current config; unverifiable changed digests fail closed.
# Coverage: tests/test_script_runtime_lifecycle.py::test_startup_migrates_exact_legacy_recovery_contract.
@runtime_checkable
class _LegacyRecoveringScriptBackend(Protocol):
    def legacy_script_recovery_signature(self) -> str:
        """Return the old backend digest for an exact, fail-closed migration check."""


# LEGACY_COMPAT: v2 backend digests without the optional seccomp field.
# Legacy format: v2 recovery signatures omitted seccomp_profile from backend config.
# Last legacy release: v2026.9.175; v2026.9.176 added seccomp_profile, including null.
# Handling: recompute only with unset seccomp, preserving every other authority field.
# Coverage: tests/test_script_runtime_lifecycle.py::test_startup_migrates_pre_seccomp_recovery_contract.
@runtime_checkable
class _PreSeccompRecoveringScriptBackend(Protocol):
    def legacy_pre_seccomp_script_recovery_signature(self) -> str | None:
        """Return the pre-field digest only when current seccomp policy is unset."""


def pre_seccomp_backend_recovery_signature(backend: WorkerBackend) -> str | None:
    """Ask a compatible backend to reproduce its exact pre-seccomp authority."""
    if isinstance(backend, _PreSeccompRecoveringScriptBackend):
        return backend.legacy_pre_seccomp_script_recovery_signature()
    return None


def legacy_pre_seccomp_recovery_digest(payload: dict[str, object]) -> str | None:
    """Omit only the historically absent, currently unset seccomp field."""
    config = cast("dict[str, object]", payload["config"])
    if config["seccomp_profile"] is not None:
        return None
    historical_config = {key: value for key, value in config.items() if key != "seccomp_profile"}
    historical_payload = {**payload, "config": historical_config}
    return hashlib.sha256(json.dumps(historical_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_legacy_script_recovery_signature(signature: str | None) -> bool:
    """Return whether a durable value has the old unversioned SHA-256 shape."""
    if signature is None or len(signature) != 64:
        return False
    return all(character in "0123456789abcdef" for character in signature)


def legacy_kubernetes_backend_recovery_signature(
    *,
    config: KubernetesWorkerBackendConfig,
    owner: str | None,
    auth_token: str | None,
    encryption_key: str | None,
    storage_root: str,
    config_snapshot: dict[str, object],
    grantable_credentials: frozenset[str],
) -> str:
    """Reproduce the old backend digest without retaining it in the current contract."""
    config_payload = asdict(config)
    config_payload.pop("image")
    config_payload.pop("image_pull_policy")
    if config_payload["runtime_class_name"] is None:
        config_payload.pop("runtime_class_name")
    payload = {
        "config": config_payload,
        "owner": owner,
        "auth_token": auth_token,
        "encryption_key": encryption_key,
        "storage_root": storage_root,
        "config_snapshot": config_snapshot,
        "grantable_credentials": sorted(grantable_credentials),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def legacy_script_recovery_signature(
    *,
    backend: WorkerBackend | None,
    config: Config,
    agent_name: str,
    gateway_url: str,
) -> str | None:
    """Reproduce the old opaque contract only to migrate an exact current match."""
    if (
        backend is None
        or backend.backend_name != "kubernetes"
        or not isinstance(backend, _LegacyRecoveringScriptBackend)
        or not gateway_url
        or agent_name not in config.agents
        or not any(tool.name == "script" for tool in config.resolve_entity(agent_name).tool_configs)
    ):
        return None
    private = config.agents[agent_name].private
    payload = {
        "protocol": SCRIPT_PROTOCOL_VERSION,
        "backend": backend.legacy_script_recovery_signature(),
        "agent": agent_name,
        "private": None if private is None else private.model_dump(mode="json"),
        "gateway": gateway_url.rstrip("/"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

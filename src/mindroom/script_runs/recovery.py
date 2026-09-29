"""Versioned authority contract for adopting an existing background script process."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mindroom.script_runs.compatibility import SCRIPT_PROTOCOL_VERSION

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.workers.backend import WorkerBackend


@runtime_checkable
class _RecoveringScriptBackend(Protocol):
    def script_recovery_signature(self) -> str:
        """Return a digest of the authority preserved by an existing worker."""

    def script_resource_recovery_authority(self, resource_profile: str | None) -> dict[str, object]:
        """Return current physical resources for one persisted profile."""


_RECOVERY_SIGNATURE_VERSION = "v2"


def script_process_authority(config: Config, agent_name: str) -> dict[str, object] | None:
    """Project configuration that determines one script worker's process authority."""
    if agent_name not in config.agents:
        return None
    agent = config.agents[agent_name]
    resolved = config.resolve_entity(agent_name)
    if not any(tool.name == "script" for tool in resolved.tool_configs):
        return None
    knowledge_paths = sorted(config.get_knowledge_base_config(base_id).path for base_id in resolved.knowledge_base_ids)
    return {
        "execution_scope": resolved.execution_scope,
        "private": None if agent.private is None else agent.private.model_dump(mode="json"),
        "knowledge_paths": knowledge_paths,
        "grantable_credentials": sorted(config.get_worker_grantable_credentials()),
    }


def script_recovery_signature(
    *,
    backend: WorkerBackend | None,
    config: Config,
    agent_name: str,
    gateway_url: str,
    resource_profile: str | None = None,
) -> str | None:
    """Bind one recoverable launch to its worker authority, private scope and gateway."""
    if (
        backend is None
        or backend.backend_name != "kubernetes"
        or not isinstance(backend, _RecoveringScriptBackend)
        or not gateway_url
        or (process_authority := script_process_authority(config, agent_name)) is None
    ):
        return None
    payload = {
        "protocol": SCRIPT_PROTOCOL_VERSION,
        "backend": backend.script_recovery_signature(),
        "agent": agent_name,
        "process_authority": process_authority,
        "gateway": gateway_url.rstrip("/"),
        "resources": backend.script_resource_recovery_authority(resource_profile),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{_RECOVERY_SIGNATURE_VERSION}:{digest}"


def verified_script_recovery_signature(
    *,
    run: ScriptRunRecord,
    backend: WorkerBackend | None,
    config: Config,
    gateway_url: str,
) -> str | None:
    """Return the current authority only when it matches the run's durable recovery digest."""
    current_signature = script_recovery_signature(
        backend=backend,
        config=config,
        agent_name=run.agent_name,
        gateway_url=gateway_url,
        resource_profile=run.resource_profile,
    )
    if current_signature is None or current_signature != run.recovery_signature:
        return None
    return current_signature
